#!/bin/bash
echo "=== Stopping and removing containers ==="
docker rm -f router_a router_b router_c 2>/dev/null || true

echo "=== Removing networks ==="
docker network rm net_lab 2>/dev/null || true

echo "Done."
