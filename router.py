import socket
import json
import threading
import time
import os
import subprocess
import ipaddress

MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n for n in os.getenv("NEIGHBORS", "").split(",") if n]
# SUBNETS: comma-separated list of CIDR blocks this router is directly connected to
# e.g. "10.0.1.0/24:10.0.1.10,10.0.3.0/24:10.0.3.10"  (subnet:local_ip pairs)
SUBNETS = os.getenv("SUBNETS", "")
PORT = 5000
INFINITY = 16
UPDATE_INTERVAL = 3
TIMEOUT = 12

routing_table = {}   # { subnet: [distance, next_hop] }
last_seen = {}       # { router_id: timestamp }
received_from = {}   # { router_id: { subnet: distance } }
src_ip_for = {}      # { router_id: src_ip }
lock = threading.Lock()


def setup_interfaces():
    """Create dummy interfaces for each directly connected subnet."""
    os.system("modprobe dummy 2>/dev/null || true")
    os.system("sysctl -w net.ipv4.conf.all.rp_filter=0 > /dev/null 2>&1")
    os.system("sysctl -w net.ipv4.conf.default.rp_filter=0 > /dev/null 2>&1")
    os.system("sysctl -w net.ipv4.ip_forward=1 > /dev/null 2>&1")
    idx = 0
    for entry in SUBNETS.split(","):
        if ":" not in entry:
            continue
        subnet, local_ip = entry.strip().split(":")
        iface = f"dum{idx}"
        prefix = subnet.split("/")[1]
        os.system(f"ip link add {iface} type dummy 2>/dev/null || true")
        # Assign as /32 so the kernel doesn't create a subnet route on the dummy
        os.system(f"ip addr add {local_ip}/32 dev {iface} 2>/dev/null || true")
        os.system(f"ip link set {iface} up")
        os.system(f"sysctl -w net.ipv4.conf.{iface}.rp_filter=0 > /dev/null 2>&1")
        # Add the subnet route pointing to eth0 (the shared bridge) so the
        # subnet is reachable and the router can forward packets for it
        os.system(f"ip route add {subnet} dev eth0 2>/dev/null || true")
        idx += 1


def get_directly_connected():
    result = subprocess.run(["ip", "-o", "-f", "inet", "addr", "show"],
                            capture_output=True, text=True)
    connected = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ip_cidr = parts[3]
            ip = ip_cidr.split("/")[0]
            if ip.startswith("127."):
                continue
            try:
                net = str(ipaddress.ip_interface(ip_cidr).network)
                # Only add each network once, using the first IP we see on it
                if net not in connected:
                    connected[net] = ip
            except Exception:
                continue
    return connected


def init_routing_table():
    if SUBNETS:
        setup_interfaces()
    time.sleep(2)  # Wait for Docker to fully configure interfaces
    connected = get_directly_connected()
    with lock:
        for subnet in connected:
            routing_table[subnet] = [0, "0.0.0.0"]
    print(f"[INIT] Directly connected: {sorted(connected.keys())}", flush=True)


def build_packet(exclude_router_id=None):
    with lock:
        routes = []
        for subnet, (dist, nexthop) in routing_table.items():
            # Split horizon with poisoned reverse: advertise INFINITY for routes learned from this neighbor
            if exclude_router_id and nexthop == exclude_router_id:
                routes.append({"subnet": subnet, "distance": INFINITY})
            else:
                routes.append({"subnet": subnet, "distance": dist})
    return json.dumps({"router_id": MY_IP, "version": 1.0, "routes": routes}).encode()


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        for neighbor in NEIGHBORS:
            try:
                pkt = build_packet(exclude_router_id=neighbor)
                sock.sendto(pkt, (neighbor, PORT))
            except Exception as e:
                pass  # Silently ignore send errors
        time.sleep(UPDATE_INTERVAL)


def trigger_update():
    """Send immediate update to all neighbors when routing table changes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for neighbor in NEIGHBORS:
        try:
            pkt = build_packet(exclude_router_id=neighbor)
            sock.sendto(pkt, (neighbor, PORT))
        except Exception:
            pass
    sock.close()


def check_timeouts():
    while True:
        time.sleep(UPDATE_INTERVAL)
        now = time.time()
        changed = False
        with lock:
            for router_id, ts in list(last_seen.items()):
                if now - ts > TIMEOUT:
                    print(f"[TIMEOUT] Neighbor {router_id} is dead", flush=True)
                    src = src_ip_for.get(router_id, router_id)
                    for subnet, (dist, hop) in list(routing_table.items()):
                        if hop == src:
                            routing_table[subnet] = [INFINITY, src]
                            subprocess.run(f"ip route del {subnet} 2>/dev/null", shell=True, capture_output=True)
                            changed = True
                    received_from.pop(router_id, None)
                    src_ip_for.pop(router_id, None)
                    last_seen.pop(router_id, None)
        if changed:
            print_table()
            trigger_update()


def update_logic(src_ip, router_id, routes_from_neighbor):
    changed = False
    with lock:
        last_seen[router_id] = time.time()
        src_ip_for[router_id] = src_ip
        received_from[router_id] = {}

        for entry in routes_from_neighbor:
            subnet = entry["subnet"]
            new_dist = min(entry["distance"] + 1, INFINITY)
            received_from[router_id][subnet] = entry["distance"]

            current_dist, current_hop = routing_table.get(subnet, [INFINITY, None])

            if new_dist < current_dist:
                routing_table[subnet] = [new_dist, src_ip]
                if new_dist < INFINITY:
                    result = subprocess.run(f"ip route replace {subnet} via {src_ip}", 
                                          shell=True, capture_output=True, text=True)
                    if result.returncode != 0:
                        print(f"[ROUTE ERROR] Failed to add route to {subnet} via {src_ip}: {result.stderr}", flush=True)
                changed = True
            elif current_hop == src_ip and new_dist != current_dist:
                routing_table[subnet] = [new_dist, src_ip]
                if new_dist >= INFINITY:
                    subprocess.run(f"ip route del {subnet} 2>/dev/null", shell=True, capture_output=True)
                else:
                    subprocess.run(f"ip route replace {subnet} via {src_ip}", shell=True, capture_output=True)
                changed = True

    if changed:
        print_table()
        trigger_update()


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    print(f"[LISTEN] Listening on UDP port {PORT}", flush=True)
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            pkt = json.loads(data.decode())
            if pkt.get("version") != 1.0:
                continue
            update_logic(addr[0], pkt["router_id"], pkt["routes"])
        except Exception as e:
            print(f"[RECV ERROR] {e}", flush=True)


def print_table():
    with lock:
        print(f"\n[TABLE] {MY_IP} @ {time.strftime('%H:%M:%S')}", flush=True)
        for subnet, (dist, hop) in sorted(routing_table.items()):
            status = "UNREACH" if dist >= INFINITY else f"dist={dist}"
            print(f"  {subnet:20s} {status:10s} via {hop}", flush=True)


if __name__ == "__main__":
    init_routing_table()
    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=check_timeouts, daemon=True).start()
    listen_for_updates()
