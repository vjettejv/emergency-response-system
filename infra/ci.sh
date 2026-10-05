#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ ! -e .env && ! -e .env.production ]] || { printf 'CI requires a clean checkout without environment files.\n' >&2; exit 1; }
[[ ${COMPOSE_PROJECT_NAME:-} == ers-ci-* ]] || { printf 'Use an isolated ers-ci-* Compose project.\n' >&2; exit 1; }
python3 - <<'PY'
from pathlib import Path
import os
import secrets
for name in ('.env', '.env.production'):
    content = Path(name + '.example').read_text()
    replacements = {
        'DJANGO_SECRET_KEY': secrets.token_urlsafe(64),
        'POSTGRES_PASSWORD': secrets.token_urlsafe(32),
        'AWS_EC2_METADATA_DISABLED': 'true',
    }
    lines = [line.split('=', 1)[0] + '=' + replacements[line.split('=', 1)[0]]
             if line.split('=', 1)[0] in replacements else line for line in content.splitlines()]
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write('\n'.join(lines) + '\n')
PY
python3 infra/check_repository.py --history
python3 -m unittest discover -s infra/tests
docker compose config --quiet
docker compose --env-file .env.production -f compose.yaml -f compose.production.yaml -f compose.https.yaml -f compose.cloudwatch.yaml config --quiet
docker compose build backend worker beat
docker compose up -d --wait --wait-timeout 90 db redis
docker compose run --rm --no-deps backend python manage.py check
docker compose run --rm --no-deps backend python manage.py migrate --noinput
docker compose run --rm --no-deps backend python manage.py makemigrations --check --dry-run
docker compose run --rm --no-deps backend python manage.py test tests --noinput
node --test backend/tests/frontend_realtime.test.cjs
