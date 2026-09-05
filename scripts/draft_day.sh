#!/usr/bin/env bash
# Draft day on the box.
#
#   scripts/draft_day.sh start    arm the kill switch, launch the loop and the
#                                 judge detached (they survive a dropped SSH)
#   scripts/draft_day.sh status   kill switch + which processes are up
#   scripts/draft_day.sh stop     disarm, kill both, kill the headless browser
#
# The judge is LIVE (Pearce, 2026-09-05 10:25): veto and within-tier reorder
# only, hard-capped in core/draft/verdict.py. The maths still drafts if it is
# late, silent, or refused. (docs/runbook-draft-day.md)
set -u
cd "$(dirname "$0")/.." || exit 1
PY=./.venv/bin/python

case "${1:-}" in
  start)
    if pgrep -f "scripts/draft.py" >/dev/null; then
      echo "a draft loop is already running — 'stop' first"; exit 1
    fi
    echo on > ENABLED
    setsid nohup $PY scripts/draft.py --judge live \
      > data/draft-live.log 2>&1 < /dev/null &
    echo "loop  pid $!"
    sleep 20
    setsid nohup $PY scripts/draft_judge.py \
      > data/judge-live.log 2>&1 < /dev/null &
    echo "judge pid $!"
    echo "armed. watch with:  tail -f data/draft-live.log"
    ;;
  status)
    echo "ENABLED=$(cat ENABLED)"
    pgrep -af "scripts/draft(_judge)?\.py" || echo "no draft processes"
    ;;
  stop)
    echo off > ENABLED
    pkill -f "scripts/draft.py" 2>/dev/null
    pkill -f "scripts/draft_judge.py" 2>/dev/null
    sleep 2
    pkill -9 -f "scripts/draft.py" 2>/dev/null
    pkill -9 -f "scripts/draft_judge.py" 2>/dev/null
    pkill -f "playwright_chromiumdev_profile" 2>/dev/null
    echo "disarmed. ENABLED=$(cat ENABLED)"
    ;;
  *)
    echo "usage: $0 start|status|stop"; exit 2
    ;;
esac
