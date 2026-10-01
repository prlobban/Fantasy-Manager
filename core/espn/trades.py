"""Incoming trade offers, read from ESPN.

The read client (`espn_api`) has no notion of a pending trade, so this goes to
the raw view. Verified 2026-09-04 against last season's league record:

    view=mTransactions2  +  x-fantasy-filter {"transactions":{"filterType":{"value":["TRADE_PROPOSAL"]}}}

returns `transactions`, each with:
    id             offer id (uuid)
    teamId         the PROPOSING team
    status         "PENDING" | "EXECUTED" | "CANCELED" | ...
    isPending      bool
    proposedDate   epoch ms
    items          [{playerId, fromTeamId, toTeamId, type: "TRADE"}, ...]

✅ VERIFIED 2026-09-30, live: a decline is NOT a status change on the offer.
ESPN writes a second record with `executionType: "CANCEL"`, `status:
"CANCELED"`, `isPending: true` and `relatedTransactionId` = the offer, and
leaves the offer itself `PENDING` until that processes. Read naively, a decline
looks like the offer still open plus a brand-new one, and the next sweep
declines it again.

A `scoringPeriodId` is required for the current season or the key is absent.
When there are no matching transactions the key is absent rather than empty;
that is read as "no offers", not as an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.espn.client import EspnClient, client

log = logging.getLogger(__name__)


@dataclass
class PendingOffer:
    offer_id: str
    from_team: int
    proposed_at: datetime
    #: espn ids arriving on OUR roster.
    incoming_ids: list[int] = field(default_factory=list)
    #: espn ids leaving OUR roster.
    outgoing_ids: list[int] = field(default_factory=list)
    status: str = "PENDING"


def pending_offers(c: EspnClient | None = None, *, my_team_id: int | None = None,
                   week: int | None = None) -> list[PendingOffer]:
    """Offers proposed TO us that are still open."""
    c = c or client()
    me = my_team_id if my_team_id is not None else c.my_team_id
    wk = week or c.current_week

    data = c.get_view(
        "mTransactions2",
        params={"scoringPeriodId": wk},
        filters={"transactions": {"filterType": {"value": ["TRADE_PROPOSAL"]}}},
    )
    raw = data.get("transactions") or []
    answered = {
        str(t["relatedTransactionId"]) for t in raw
        if t.get("relatedTransactionId") and str(t.get("executionType", "")).upper() == "CANCEL"
    }
    out: list[PendingOffer] = []
    for t in raw:
        if str(t.get("executionType", "")).upper() == "CANCEL":
            continue  # a decline or withdrawal record, not an offer
        if str(t.get("id")) in answered:
            continue  # already declined or withdrawn; ESPN just hasn't processed it
        status = str(t.get("status", "")).upper()
        if status != "PENDING" and not (status == "" and t.get("isPending")):
            continue
        proposer = int(t.get("teamId", -1))
        if proposer == me:
            continue  # our own outgoing proposal
        items = t.get("items") or []
        incoming = [int(i["playerId"]) for i in items if int(i.get("toTeamId", -1)) == me]
        outgoing = [int(i["playerId"]) for i in items if int(i.get("fromTeamId", -1)) == me]
        if not incoming and not outgoing:
            continue  # a trade between two other teams
        ms = t.get("proposedDate")
        out.append(PendingOffer(
            offer_id=str(t.get("id")),
            from_team=proposer,
            proposed_at=(datetime.fromtimestamp(ms / 1000, tz=UTC) if ms
                         else datetime.now(UTC)),
            incoming_ids=incoming,
            outgoing_ids=outgoing,
            status=status or "PENDING",
        ))
    log.info("pending trade offers to us: %d", len(out))
    return out
