#!/bin/bash
# test.sh — Full convergence and failover test for the Distance-Vector Router

set -e

IMAGE="my-router"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== [1] Creating lab network ==="
# Single bridge network for container-to-container UDP communication.
# Each container adds its own dummy interfaces to simulate subnet membership.
docker network create --subnet=172.20.0.0/24 net_lab 2>/dev/null || echo "net_lab already exists"

echo ""
echo "=== [2] Building router image ==="
docker build -t "$IMAGE" "$DIR"

echo ""
echo "=== [3] Starting routers ==="
# Topology:
#   Router A: net_ab (10.0.1.10), net_ac (10.0.3.10)  — management IP 172.20.0.10
#   Router B: net_ab (10.0.1.20), net_bc (10.0.2.10)  — management IP 172.20.0.20
#   Router C: net_bc (10.0.2.20), net_ac (10.0.3.30)  — management IP 172.20.0.30
#
# NEIGHBORS env uses management IPs for UDP reachability.
# SUBNETS env defines which logical subnets each router owns (subnet:local_ip pairs).

docker run -d --name router_a --privileged \
  --network net_lab --ip 172.20.0.10 \
  -e MY_IP=172.20.0.10 \
  -e NEIGHBORS=172.20.0.20,172.20.0.30 \
  -e SUBNETS="10.0.1.0/24:10.0.1.10,10.0.3.0/24:10.0.3.10" \
  "$IMAGE"

docker run -d --name router_b --privileged \
  --network net_lab --ip 172.20.0.20 \
  -e MY_IP=172.20.0.20 \
  -e NEIGHBORS=172.20.0.10,172.20.0.30 \
  -e SUBNETS="10.0.1.0/24:10.0.1.20,10.0.2.0/24:10.0.2.10" \
  "$IMAGE"

docker run -d --name router_c --privileged \
  --network net_lab --ip 172.20.0.30 \
  -e MY_IP=172.20.0.30 \
  -e NEIGHBORS=172.20.0.10,172.20.0.20 \
  -e SUBNETS="10.0.2.0/24:10.0.2.20,10.0.3.0/24:10.0.3.30" \
  "$IMAGE"

echo ""
echo "=== [4] Waiting 20s for initial convergence ==="
sleep 20

echo ""
echo "=== [TEST 1] Routing tables after convergence ==="
for r in router_a router_b router_c; do
    echo "--- $r ---"
    docker logs "$r" 2>&1 | grep -E "\[TABLE\]|10\.0\." | tail -12
done

echo ""
echo "=== [TEST 2] Kernel routing tables ==="
for r in router_a router_b router_c; do
    echo "--- $r ---"
    docker exec "$r" ip route
done

echo ""
echo "=== [TEST 3] Ping tests ==="
echo "A -> C direct (10.0.3.30, 1 hop via net_ac):"
docker exec router_a ping -c 4 10.0.3.30 && echo "PASS" || echo "FAIL"

echo ""
echo "A -> B direct (10.0.1.20, 1 hop via net_ab):"
docker exec router_a ping -c 4 10.0.1.20 && echo "PASS" || echo "FAIL"

echo ""
echo "B -> C direct (10.0.2.20, 1 hop via net_bc):"
docker exec router_b ping -c 4 10.0.2.20 && echo "PASS" || echo "FAIL"

echo ""
echo "A -> C net_bc addr (10.0.2.20, 2 hops via B):"
docker exec router_a ping -c 4 10.0.2.20 && echo "PASS" || echo "FAIL"

echo ""
echo "C -> A net_ab addr (10.0.1.10, 2 hops via B):"
docker exec router_c ping -c 4 10.0.1.10 && echo "PASS" || echo "FAIL"

echo ""
echo "=== [TEST 4] Stopping Router C (node failure) ==="
docker stop router_c
echo "Waiting 20s for timeout and re-convergence..."
sleep 20

echo ""
echo "=== [TEST 5] Tables after Router C failure ==="
for r in router_a router_b; do
    echo "--- $r ---"
    docker logs "$r" 2>&1 | grep -E "TIMEOUT|UNREACH|\[TABLE\]|10\.0\." | tail -15
done

echo ""
echo "=== [TEST 6] Restarting Router C — re-convergence ==="
docker start router_c
sleep 20

echo "--- router_a after C restart ---"
docker logs router_a 2>&1 | grep -E "\[TABLE\]|10\.0\." | tail -12

echo ""
echo "=== All tests done ==="
echo "Run ./cleanup.sh to tear down."
