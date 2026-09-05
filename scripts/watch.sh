#!/usr/bin/env bash
# Live view of the draft from the box: our current queue and the last events.
cd "$(dirname "$0")/.." || exit 1
D=$(ls -td data/drafts/*-live/ 2>/dev/null | head -1)
while true; do
  clear
  echo "== $(date +%H:%M:%S)  ENABLED=$(cat ENABLED)  $(pgrep -f scripts/draft.py >/dev/null && echo LOOP-UP || echo LOOP-DOWN)"
  echo "== clock: $(cat $D/clock.json 2>/dev/null)"
  echo "== QUEUE (top of our ESPN queue, newest sync):"
  grep queue_sync "$D/events.jsonl" | tail -1 | ./.venv/bin/python -c "import sys,json; e=json.loads(sys.stdin.read()); [print(f\"  {i+1}. {n}\") for i,n in enumerate(e[\"target\"])]; print(f\"  landed {e[\"landed\"]}/{e[\"planned\"]} at {e[\"at\"][11:19]}Z\")" 2>/dev/null
  echo "== OUR PICKS:"; grep "EXECUTED draft_pick" data/draft-live.log | sed "s/.*round/  round/"
  echo "== LAST LOG:"; tail -4 data/draft-live.log | cut -c1-150
  sleep 5
done
