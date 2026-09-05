#!/usr/bin/env bash
# One League-Specific Practice Draft, from a RANDOM seat (or $1), judge in
# shadow, loop in the foreground so you can watch it. Ends disarmed.
#
# ESPN allows ONE practice room per account: opening another one while this
# runs displaces the bot's room (observed 2026-09-04).
set -u
cd "$(dirname "$0")/.." || exit 1
PY=./.venv/bin/python
SLOT="${1:-$((RANDOM % 10 + 1))}"
LOG="data/practice-s${SLOT}-$(date -u +%H%M%S).log"
echo on > ENABLED
echo "slot $SLOT -> $LOG"
setsid nohup $PY scripts/draft_judge.py --shadow \
  > "data/practice-judge.log" 2>&1 < /dev/null &
JPID=$!
timeout 900 $PY scripts/draft.py --practice --practice-slot "$SLOT" \
  --judge shadow --max-minutes 14 2>&1 | tee "$LOG"
kill "$JPID" 2>/dev/null; sleep 2; kill -9 "$JPID" 2>/dev/null
echo off > ENABLED
P=$(grep -c "EXECUTED draft_pick" "$LOG")
B=$(grep -c "STOOD DOWN" "$LOG")
C=$(grep -c "clicked three ways" "$LOG")
echo "RESULT slot=$SLOT picks=$P/13 breaker=$B clickfail=$C  (ENABLED=$(cat ENABLED))"
