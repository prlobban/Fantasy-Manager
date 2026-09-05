#!/usr/bin/env bash
# Provision Fantasy-Manager on the OptiPlex box. Idempotent — safe to re-run.
set -euo pipefail

PROJ="$HOME/Fantasy-Manager"
cd "$PROJ"

echo "== python venv =="
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e ".[dev]"

echo "== playwright chromium =="
# System libs need sudo and are installed separately — see docs/build-plan.md.
./.venv/bin/playwright install chromium

echo "== dirs =="
mkdir -p data/cache data/screenshots data/agent-runs

echo "== kill switch (ships OFF) =="
[ -f ENABLED ] || echo "off" > ENABLED
echo "ENABLED = $(head -1 ENABLED)"

echo "== .env =="
if [ ! -f .env ]; then
  echo "!! .env is MISSING. Copy .env.example and fill in ESPN_SWID / ESPN_S2."
  echo "   Nothing will run without it."
else
  echo ".env present ($(grep -c '=' .env) keys)"
fi

echo "== saved web session =="
if [ ! -f data/espn-session.json ]; then
  echo "!! data/espn-session.json MISSING — every WRITE will fail."
  echo "   The API cookies are not enough for the web UI. On the laptop run:"
  echo "     python scripts/login.py && python scripts/login.py --verify"
  echo "     scp data/espn-session.json ironman@192.168.4.43:~/Fantasy-Manager/data/"
else
  echo "session file present"
fi

echo "== tests =="
./.venv/bin/python -m pytest -q -m "not live and not browser" || true

echo "== health =="
./.venv/bin/python scripts/healthcheck.py --no-kill || true

cat <<'NOTE'

== cron (add by hand with `crontab -e`) ==
CRON_TZ=America/Chicago
30 7 * * *  cd $HOME/Fantasy-Manager && ./.venv/bin/python scripts/manage.py >> data/manage.log 2>&1
0 11 * * 0  cd $HOME/Fantasy-Manager && ./.venv/bin/python scripts/manage.py --task lineup >> data/manage.log 2>&1
0 9 * * 2   cd $HOME/Fantasy-Manager && ./.venv/bin/python scripts/manage.py --tuesday >> data/manage.log 2>&1

NOT installed automatically, deliberately: cron on a live league should be a
decision someone makes on purpose.
NOTE
