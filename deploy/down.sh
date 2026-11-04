#!/bin/sh
# Stop the manager (agent containers are removed via the portal or with:
# docker rm -f $(docker ps -aq --filter label=doppio.agent-id) ).
set -eu
cd "$(dirname "$0")/.."
docker compose -f deploy/docker-compose.yml down
