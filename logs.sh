#!/bin/bash
cd "$(dirname "${BASH_SOURCE[0]}")/docker"
docker compose -f docker-compose.multi.yml logs -f
