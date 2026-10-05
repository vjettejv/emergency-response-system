#!/usr/bin/env bash
set -Eeuo pipefail
set +x
for name in EC2_HOST EC2_USER EC2_SSH_KEY EC2_KNOWN_HOSTS GH_TOKEN REPOSITORY EXPECTED_SHA; do
    [[ -n ${!name:-} ]] || { printf 'Missing required deployment setting: %s\n' "$name" >&2; exit 1; }
done
[[ $EC2_HOST =~ ^[a-zA-Z0-9.-]+$ && $EC2_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || exit 1
[[ $EXPECTED_SHA =~ ^[a-f0-9]{40}$ && $REPOSITORY =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ ]] || exit 1
private_dir=$(mktemp -d)
trap 'rm -rf -- "$private_dir"' EXIT
chmod 700 "$private_dir"
printf '%s\n' "$EC2_SSH_KEY" > "$private_dir/key"
printf '%s\n' "$EC2_KNOWN_HOSTS" > "$private_dir/known_hosts"
chmod 600 "$private_dir/key" "$private_dir/known_hosts"
# The forced command accepts bounded JSON only, never shell code or token arguments.
python3 - <<'PY' | ssh -i "$private_dir/key" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$private_dir/known_hosts" \
    -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 \
    "$EC2_USER@$EC2_HOST" \
    ers-deploy
import json
import os
import sys
json.dump({'repository': os.environ['REPOSITORY'], 'expected_sha': os.environ['EXPECTED_SHA'],
           'github_token': os.environ['GH_TOKEN']}, sys.stdout)
PY
