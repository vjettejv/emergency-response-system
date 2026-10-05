#!/usr/bin/env bash
# Install root-owned under /etc/letsencrypt/renewal-hooks/deploy/ on this EC2.
set -Eeuo pipefail
cd /opt/emergency
docker compose --env-file .env.production -f compose.yaml -f compose.production.yaml -f compose.https.yaml -f compose.cloudwatch.yaml exec -T nginx nginx -t
docker compose --env-file .env.production -f compose.yaml -f compose.production.yaml -f compose.https.yaml -f compose.cloudwatch.yaml exec -T nginx nginx -s reload
