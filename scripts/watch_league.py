"""The fast lane: every five minutes, no model unless something happened (§8.10).

    python scripts/watch_league.py            # one tick (cron, */5)
    python scripts/watch_league.py --dry-run  # say what it would do, trigger nothing

ESPN has no webhooks, so "wake on a trade offer" is a poll. This one is cheap:
two reads of ESPN and one of Slack, and it costs nothing when nothing changed.
It does three things:

1. **Sends approved chat drafts** (core.chat_drafts.process) — no model.
2. **Wakes the sweep on a new incoming trade offer.** The sweep already
   answers offers through the §6.8 gauntlet; it just used to see them once a
   morning, so an offer at 13:00 waited until 07:30 tomorrow.
3. **Wakes the sweep when someone is waiting on a reply in chat**: their last
   line is newer than ours and newer than anything already handled. A burst
   ("Good morning" / "would you…" / "I'll give you…") is waited out until it
   has been quiet for SETTLE_S, so three lines cost one sweep. If Pearce has
   answered himself, nothing is owed and nothing fires.

The triggered sweep is the ordinary one (`cron_manage.sh sweep`), so every
gate, rate limit and the kill switch apply exactly as at 07:30. Spacing and a
daily cap bound the cost: chat is not allowed to turn the manager into a
chatbot billing a sweep per message.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import REPO_ROOT, settings  # noqa: E402

log = logging.getLogger("watch")

SETTLE_S = 240
MIN_GAP = timedelta(minutes=20)
MAX_TRIGGERS_PER_DAY = 10


def _state_path() -> Path:
    return settings().data_dir / "watch.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(s: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def decide(state: dict, *, comms, offer_ids: list[str] | None, now: datetime) -> tuple[list[str], dict]:
    """Pure: what happened since last tick → (reasons to wake the sweep, new state).

    `offer_ids` None means the offer read failed — nothing is concluded from it.
    First tick seeds the state from the present and wakes nothing: history is
    not news.
    """
    s = dict(state)
    now_ms = int(now.timestamp() * 1000)
    reasons: list[str] = []
    first = "handled_ms" not in s

    if comms is not None and comms.ok:
        handled = int(s.get("handled_ms") or 0)
        newest_settled = handled
        for t in comms.topics:
            last_in = t.last_inbound
            if not last_in or last_in.at_ms <= handled:
                continue
            if not t.awaiting_us():
                newest_settled = max(newest_settled, last_in.at_ms)  # he answered himself
                continue
            if now_ms - last_in.at_ms < SETTLE_S * 1000:
                continue  # still typing — next tick
            newest_settled = max(newest_settled, last_in.at_ms)
            if not first:
                who = ", ".join(t.with_names)
                reasons.append(f"chat: {who} is waiting on a reply")
        if first:
            newest_settled = max([handled] + [ln.at_ms for t in comms.topics for ln in t.lines])
        s["handled_ms"] = newest_settled

    if offer_ids is not None:
        seen = set(s.get("seen_offers") or [])
        new = [o for o in offer_ids if o not in seen]
        if new and "seen_offers" in s:
            reasons.append(f"trade: {len(new)} new incoming offer(s)")
        s["seen_offers"] = sorted(seen | set(offer_ids))[-100:]

    return reasons, s


def allowed(state: dict, now: datetime) -> tuple[bool, str]:
    fired = [datetime.fromisoformat(x) for x in state.get("triggers") or []]
    today = [f for f in fired if f.date() == now.date()]
    if len(today) >= MAX_TRIGGERS_PER_DAY:
        return False, f"daily cap {MAX_TRIGGERS_PER_DAY} reached"
    if fired and now - max(fired) < MIN_GAP:
        return False, "last triggered sweep under 20 min ago"
    return True, ""


def _sweep_running() -> bool:
    """The sweep holds data/run.lock for its whole run."""
    p = settings().data_dir / "run.lock"
    if not p.exists():
        return False
    with p.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def main() -> int:
    # 288 ticks a day: the libraries speak only on a warning, the watcher only
    # when it acts. A quiet tick writes nothing.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from core import chat_drafts
    from core.espn import chat, trades

    if not args.dry_run:
        for line in chat_drafts.process():
            log.info("drafts: %s", line)

    comms = chat.read()
    if not comms.ok:
        log.warning("chat unreadable: %s", comms.error)
    try:
        offer_ids = [o.offer_id for o in trades.pending_offers()]
    except Exception as e:
        log.warning("offer read failed: %s", e)
        offer_ids = None

    now = datetime.now(UTC)
    state = load_state()
    reasons, new_state = decide(state, comms=comms, offer_ids=offer_ids, now=now)

    if not reasons:
        if not args.dry_run:
            save_state(new_state)
        return 0

    ok, why_not = allowed(state, now)
    if not ok or _sweep_running():
        # Leave the state unadvanced so the next tick tries again — except at
        # the daily cap, where retrying every five minutes is just noise.
        log.info("would wake the sweep (%s) but %s", "; ".join(reasons),
                 why_not or "a sweep is already running")
        if why_not.startswith("daily cap") and not args.dry_run:
            save_state(new_state)
        return 0

    log.info("waking the sweep: %s", "; ".join(reasons))
    if args.dry_run:
        return 0
    new_state["triggers"] = ([x for x in state.get("triggers") or []
                              if datetime.fromisoformat(x) > now - timedelta(days=2)]
                             + [now.isoformat()])
    save_state(new_state)
    with open(settings().data_dir / "manager.log", "a", encoding="utf-8") as f:
        f.write(f"{now.isoformat()} watch: waking the sweep — {'; '.join(reasons)}\n")
    subprocess.Popen([str(REPO_ROOT / "scripts" / "cron_manage.sh"), "sweep"],
                     cwd=str(REPO_ROOT), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
