#!/usr/bin/env bash
set -Eeuo pipefail
set +x
[[ ${EXPECTED_SHA:-} =~ ^[a-f0-9]{40}$ && ${REPOSITORY:-} =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ && -n ${GH_TOKEN:-} ]] || exit 1
cd /opt/emergency
[[ -d .git && -f .env.production ]] || { printf 'EC2 Git checkout is not initialized.\n' >&2; exit 1; }
exec 8>.cd.lock
flock -n 8 || { printf 'Another CD deployment is active.\n' >&2; exit 1; }
[[ $(git branch --show-current) == main ]] || exit 1
git diff --quiet && git diff --cached --quiet || { printf 'Tracked EC2 files have local changes.\n' >&2; exit 1; }
[[ $(git remote get-url origin) == "https://github.com/$REPOSITORY.git" ]] || exit 1
unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE GIT_TRACE_PACKET
export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=http.https://github.com/.extraheader
export GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)"
export GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1=''
unset GH_TOKEN
git pull --ff-only origin main
unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1
[[ $(git rev-parse HEAD) == "$EXPECTED_SHA" ]] || { printf 'Main advanced beyond the tested commit; refusing deployment.\n' >&2; exit 1; }
export APP_TAG="$EXPECTED_SHA"
sudo -n env APP_TAG="$APP_TAG" WITH_HTTPS=1 WITH_CLOUDWATCH=1 bash infra/deploy.sh --no-pull
python3 - <<'PY'
from pathlib import Path
import os
path = Path('.env.production')
lines = path.read_text().splitlines()
tag = "APP_TAG='" + os.environ['APP_TAG'] + "'"
lines = [tag if line.startswith('APP_TAG=') else line for line in lines]
if not any(line.startswith('APP_TAG=') for line in lines):
    lines.append(tag)
temporary = path.with_name('.env.production.tmp')
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    stream.write('\n'.join(lines) + '\n')
temporary.replace(path)
PY
