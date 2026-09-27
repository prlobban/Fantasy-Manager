#!/usr/bin/env bash
# The manager's cron entry point on the box. One task per invocation:
#
#   scripts/cron_manage.sh research      07:00 daily   — morning dossiers
#   scripts/cron_manage.sh sweep         07:30 daily   — the full sweep
#   scripts/cron_manage.sh tuesday       07:30 Tue     — review, then the sweep
#   scripts/cron_manage.sh sweep         Sun 11:00     — second game-day sweep, pre-kickoff
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
# Where this run's slice of the log starts — self-repair reads only that.
OFFSET="$(stat -c %s "$LOG" 2>/dev/null || echo 0)"
echo "$TS ── $TASK (ENABLED=$(cat ENABLED 2>/dev/null))" >> "$LOG"

# The run lock. scripts/repair.py takes it to deploy or revert, so code is
# never swapped out from under a running sweep. A task waits up to 30 min.
mkdir -p data/repair
exec 9>data/run.lock
flock -w 1800 9 || echo "$(date -Is) ⚠️ run lock wait timed out — running anyway" >> "$LOG"

case "$TASK" in
  research) $PY scripts/research_week.py >> "$LOG" 2>&1 ;;
  sweep)    $PY scripts/manage.py >> "$LOG" 2>&1 ;;
  tuesday)  $PY scripts/manage.py --tuesday >> "$LOG" 2>&1
            $PY scripts/manage.py >> "$LOG" 2>&1 ;;
  lineup)   $PY scripts/manage.py --task lineup >> "$LOG" 2>&1 ;;
  *) echo "usage: $0 research|sweep|tuesday|lineup" >&2; exit 2 ;;
esac
RC=$?
flock -u 9
if [ "$RC" -ne 0 ] || tail -60 "$LOG" | grep -qiE 'session limit|usage limit|rate limit|overloaded|credit balance'; then
  echo "$(date -Is) ⚠️ $TASK FAILED (rc=$RC)" >> "$LOG"
  # Carry the reason, not a pointer. "see data/manager.log on the box" is what
  # an expired login looked like for two days: an alert that fires correctly
  # and says nothing you can act on.
  WHY="$(grep -iE 'AGENT FAILED|FAILED:|auth:|capacity:|Traceback|Error' "$LOG" | tail -5)"
  WHY="${WHY:-see data/manager.log on the box}" \
  $PY - <<'PY' >> "$LOG" 2>&1 || true
import os
from core.notify import notify
notify("error", "Fantasy manager: cron task failed",
       os.environ.get("WHY", "see data/manager.log on the box"))
PY
fi
echo "$(date -Is) done $TASK (rc=$RC)" >> "$LOG"

# Self-repair (Pearce, 2026-09-27: all-encompassing, auto-deploy, tell me
# after). Whatever this run reported as wrong becomes a tested fix, deployed,
# or a note on why not — see scripts/repair.py. Never fails this cron line.
# REPAIR=off in the environment skips it.
if [ "${REPAIR:-on}" != "off" ]; then
  timeout 3600 $PY scripts/repair.py --task "$TASK" --rc "$RC" \
    --log-offset "$OFFSET" --since "$TS" >> data/repair/repair.log 2>&1 || true
fi
exit 0
