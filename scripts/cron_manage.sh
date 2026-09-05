#!/usr/bin/env bash
# The manager's cron entry point on the box. One task per invocation:
#
#   scripts/cron_manage.sh research      07:00 daily   — morning dossiers
#   scripts/cron_manage.sh sweep         07:30 daily   — the full sweep
#   scripts/cron_manage.sh tuesday       07:30 Tue     — review, then the sweep
#   scripts/cron_manage.sh lineup        Thu/Sun/Mon   — lineup only
#
# Every run is gated by ENABLED: with it off the sweep still runs and still
# posts, but every write is refused (§8.4). That is the "test it Monday" mode.
# Capacity notices from claude are sniffed, not trusted to the exit code.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="$HOME/.npm-global/bin:$PATH"
PY=./.venv/bin/python
LOG=data/manager.log
TASK="${1:-sweep}"
TS="$(date -Is)"
echo "$TS ── $TASK (ENABLED=$(cat ENABLED 2>/dev/null))" >> "$LOG"

case "$TASK" in
  research) $PY scripts/research_week.py >> "$LOG" 2>&1 ;;
  sweep)    $PY scripts/manage.py >> "$LOG" 2>&1 ;;
  tuesday)  $PY scripts/manage.py --tuesday >> "$LOG" 2>&1
            $PY scripts/manage.py >> "$LOG" 2>&1 ;;
  lineup)   $PY scripts/manage.py --task lineup >> "$LOG" 2>&1 ;;
  *) echo "usage: $0 research|sweep|tuesday|lineup" >&2; exit 2 ;;
esac
RC=$?
if [ "$RC" -ne 0 ] || tail -60 "$LOG" | grep -qiE 'session limit|usage limit|rate limit|overloaded|credit balance'; then
  echo "$(date -Is) ⚠️ $TASK FAILED (rc=$RC)" >> "$LOG"
  $PY - <<'PY' >> "$LOG" 2>&1 || true
from core.notify import notify
import sys
notify("error", "Fantasy manager: cron task failed", "see data/manager.log on the box")
PY
fi
echo "$(date -Is) done $TASK (rc=$RC)" >> "$LOG"
exit 0
