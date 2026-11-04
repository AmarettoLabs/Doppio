#!/bin/sh
# Start Doppio: build images, ensure the shared network, run compose.
# Idempotent: safe to re-run.
set -eu
cd "$(dirname "$0")/.."

echo "== building agent image"
docker build -f deploy/Dockerfile.agent -t doppio-agent:latest .

echo "== ensuring shared network doppio_agents"
docker network inspect doppio_agents >/dev/null 2>&1 || docker network create doppio_agents

echo "== starting manager"
docker compose -f deploy/docker-compose.yml up -d --build

sleep 2
curl -sf http://127.0.0.1:8080/api/health && echo
echo "portal: http://localhost:8080"
