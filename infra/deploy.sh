#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
trap 'printf "Deployment failed; inspect container status/logs. No volumes were removed.\n" >&2' ERR
[[ -f .env.production ]] || { printf 'Create .env.production first.\n' >&2; exit 1; }
[[ -f .certificates/rds-global-bundle.pem ]] || { printf 'Install the RDS CA bundle first.\n' >&2; exit 1; }
# Do not source the secret file as shell code or print interpolated Compose config.
if [[ -n ${DEPLOY_LOCK_FD:-} ]]; then
    [[ $DEPLOY_LOCK_FD =~ ^[0-9]+$ && -e /proc/$$/fd/$DEPLOY_LOCK_FD ]] || exit 1
else
    exec 9>.deploy.lock
    flock -n 9 || { printf 'Another deployment is running.\n' >&2; exit 1; }
fi
record_phase() { if [[ -n ${DEPLOY_PHASE_FILE:-} ]]; then printf '%s\n' "$1" > "$DEPLOY_PHASE_FILE"; fi; }
if [[ ${1:-} != --no-pull ]]; then
    [[ $# == 0 ]] || { printf 'Usage: bash infra/deploy.sh [--no-pull]\n' >&2; exit 1; }
    git pull --ff-only
fi
[[ $# -le 1 ]] || { printf 'Usage: bash infra/deploy.sh [--no-pull]\n' >&2; exit 1; }
compose=(docker compose --env-file .env.production -f compose.yaml -f compose.production.yaml)
if [[ ${WITH_HTTPS:-0} == 1 ]]; then compose+=(-f compose.https.yaml); fi
if [[ ${WITH_CLOUDWATCH:-0} == 1 ]]; then compose+=(-f compose.cloudwatch.yaml); fi
record_phase config
"${compose[@]}" config --quiet
record_phase build
"${compose[@]}" build initialize
record_phase check
"${compose[@]}" run --rm --no-deps -e DEPLOY_HTTPS="${WITH_HTTPS:-0}" initialize python -c 'import os, sys; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings"); import django; django.setup(); from django.conf import settings; from django.core.management import call_command; configured = os.environ["DEPLOY_HTTPS"] == "1"; settings.HTTPS_ENABLED == configured or sys.exit("DJANGO_HTTPS_ENABLED and WITH_HTTPS must match."); call_command("check", deploy=True)'
if [[ -n ${DEPLOY_MIGRATION_PLAN_FILE:-} ]]; then
    "${compose[@]}" run --rm --no-deps initialize python -c 'import os,json; os.environ.setdefault("DJANGO_SETTINGS_MODULE","config.settings"); import django; django.setup(); from django.db import connection; from django.db.migrations.executor import MigrationExecutor; executor=MigrationExecutor(connection); print(json.dumps([(migration.app_label,migration.name,backwards) for migration,backwards in executor.migration_plan(executor.loader.graph.leaf_nodes())]))' > "$DEPLOY_MIGRATION_PLAN_FILE"
fi
record_phase redis
"${compose[@]}" up -d --wait --wait-timeout 90 redis
# Short maintenance window; prevent old processes from using a changing schema.
record_phase stop
"${compose[@]}" stop nginx backend worker beat
record_phase migrate
"${compose[@]}" up --no-deps --force-recreate --exit-code-from initialize initialize
record_phase services
"${compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 180 backend worker beat
record_phase nginx
"${compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 90 nginx
record_phase readiness
"${compose[@]}" exec -T backend python manage.py check_infrastructure
"${compose[@]}" exec -T nginx nginx -t
printf 'Containers and internal readiness passed. Run public HTTPS/WSS/S3/mobile smoke tests next.\n'
"${compose[@]}" ps
if [[ ${WITH_HTTPS:-0} == 1 ]]; then
    record_phase health
    # Parse only the public host; never source or print the secret environment.
    public_host=$("${compose[@]}" exec -T backend python -c 'import os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings"); from django.conf import settings; print(settings.ALLOWED_HOSTS[0])')
    [[ $public_host =~ ^[a-zA-Z0-9.-]+$ ]] || exit 1
    curl --fail --silent --show-error --retry 5 --retry-delay 2 --retry-connrefused \
        --connect-timeout 10 --max-time 20 "https://$public_host/api/health/" \
        | python3 -c 'import json,sys; data=json.load(sys.stdin); all(data.get(k)=="ok" for k in ("status","database","redis")) or sys.exit(1)'
    printf 'Public HTTPS health check passed.\n'
fi
record_phase complete
