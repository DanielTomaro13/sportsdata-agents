"""Shadow account: your ACTUAL bets audited against the alert stream.

The scoreboard answers "what were the alerts worth if you'd taken them all";
this module answers the personal questions it can't: which alerts did you
actually bet, at what price relative to the quoted one, staked how far from
the printed Kelly — and how did your real P&L compare to the flat-take
counterfactual. (Idea borrowed from Vibe-Trading's broker-journal shadow
account, translated from equities to betting.)

Input is a bet journal CSV — bookmaker export or hand-kept — with flexible
headers. Recognised (case-insensitive, first match wins):

- placed:    placed_at / placed / date / time / bet_time
- event:     event / match / race / fixture / market_name
- selection: selection / runner / pick / bet / outcome_name
- odds:      odds / price / avg_odds
- stake:     stake / amount / risk / wagered
- result:    result / outcome / status        (win/won/lost/loss/void/refunded)
- returned:  return / returns / payout / collected   (optional; pnl derived)

Matching a bet to an alert is deliberately dumb and transparent: normalised
selection-name containment in the alert MESSAGE, inside a time window
(alert fired up to 24h before the bet, 15min slop the other way). Every
unmatched bet is reported as your own pick, never silently dropped.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import Alert

__all__ = ["format_shadow_report", "parse_bets", "shadow_report"]

_PLACED = ("placed_at", "placed", "date", "time", "bet_time")
_EVENT = ("event", "match", "race", "fixture", "market_name")
_SELECTION = ("selection", "runner", "pick", "bet", "outcome_name")
_ODDS = ("odds", "price", "avg_odds")
_STAKE = ("stake", "amount", "risk", "wagered")
_RESULT = ("result", "outcome", "status")
_RETURNED = ("return", "returns", "payout", "collected")

_WIN_WORDS = {"win", "won", "winner", "paid"}
_LOSS_WORDS = {"loss", "lost", "lose", "unplaced"}
_VOID_WORDS = {"void", "refund", "refunded", "cancelled", "canceled", "scratched"}

_MATCH_BEFORE = dt.timedelta(hours=24)   # alert may precede the bet this long
_MATCH_AFTER = dt.timedelta(minutes=15)  # clock-slop the other way


@dataclass
class Bet:
    placed_at: dt.datetime | None
    event: str
    selection: str
    odds: float | None
    stake: float | None
    result: str          # win | loss | void | unknown
    pnl: float | None    # settled P&L in stake currency; None when unknowable
    row: int             # 1-based CSV line, for the report's receipts
    alert_id: str | None = None
    alert_kind: str | None = None
    alert_odds: float | None = None
    alert_kelly: float | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _pick_column(headers: list[str], wanted: tuple[str, ...]) -> str | None:
    lowered = {h.lower().strip(): h for h in headers}
    for w in wanted:
        if w in lowered:
            return lowered[w]
    return None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    except ValueError:
        return None


def _to_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            parsed = (dt.datetime.fromisoformat(text) if fmt is None
                      else dt.datetime.strptime(text, fmt))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            continue
    return None


def parse_bets(path: str | Path) -> list[Bet]:
    """Journal CSV -> normalised bets. Raises ValueError when the file has no
    recognisable selection or odds column — a wrong file should say so, not
    produce an empty report that reads as 'no bets'."""
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        if not headers:
            raise ValueError("empty journal: no header row")
        col_placed = _pick_column(headers, _PLACED)
        col_event = _pick_column(headers, _EVENT)
        col_sel = _pick_column(headers, _SELECTION)
        col_odds = _pick_column(headers, _ODDS)
        col_stake = _pick_column(headers, _STAKE)
        col_result = _pick_column(headers, _RESULT)
        col_ret = _pick_column(headers, _RETURNED)
        if col_sel is None or col_odds is None:
            raise ValueError(
                f"journal headers {headers} carry no selection/odds column — "
                f"expected one of {_SELECTION} and one of {_ODDS}")
        bets: list[Bet] = []
        for i, row in enumerate(reader, start=2):  # 1 is the header line
            selection = str(row.get(col_sel) or "").strip()
            if not selection:
                continue
            odds = _to_float(row.get(col_odds))
            stake = _to_float(row.get(col_stake)) if col_stake else None
            returned = _to_float(row.get(col_ret)) if col_ret else None
            raw_result = _norm(str(row.get(col_result) or "")) if col_result else ""
            if raw_result in _VOID_WORDS:
                result = "void"
            elif raw_result in _WIN_WORDS:
                result = "win"
            elif raw_result in _LOSS_WORDS:
                result = "loss"
            elif returned is not None and stake is not None:
                result = "void" if returned == stake else (
                    "win" if returned > 0 else "loss")
            else:
                result = "unknown"
            pnl: float | None = None
            if result == "void":
                pnl = 0.0
            elif returned is not None and stake is not None:
                pnl = returned - stake
            elif result == "win" and stake is not None and odds is not None:
                pnl = stake * (odds - 1.0)
            elif result == "loss" and stake is not None:
                pnl = -stake
            bets.append(Bet(
                placed_at=_to_dt(row.get(col_placed)) if col_placed else None,
                event=str(row.get(col_event) or "").strip() if col_event else "",
                selection=selection, odds=odds, stake=stake,
                result=result, pnl=pnl, row=i))
        return bets


def _alert_quote(kind: str, payload: dict[str, Any]) -> tuple[float | None, float | None]:
    """(quoted odds, printed kelly stake) for an alert, per kind's payload shape."""
    if kind == "bsp_value":
        runners = payload.get("runners") or []
        top = runners[0] if runners and isinstance(runners[0], dict) else {}
        return _f(top.get("back")), _f(top.get("kelly_stake"))
    if kind == "model_value":
        cands = payload.get("candidates") or []
        top = cands[0] if cands and isinstance(cands[0], dict) else {}
        return _f(top.get("odds")), None  # flat-graded kind prints no kelly
    if kind == "prediction_value":
        return _f(payload.get("back_odds")), _f(payload.get("kelly_stake"))
    return _f(payload.get("odds")), _f(payload.get("kelly_stake"))


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def shadow_report(
    session: AsyncSession,
    bets: list[Bet],
) -> dict[str, Any]:
    """Join the journal against the alerts table and audit the overlap."""
    stamped_times = [b.placed_at for b in bets if b.placed_at is not None]
    if stamped_times:
        lo = min(stamped_times) - _MATCH_BEFORE
        hi = max(stamped_times) + _MATCH_AFTER
        alerts = (await session.execute(
            select(Alert).where(Alert.created_at >= lo, Alert.created_at <= hi)
        )).scalars().all()
    else:
        alerts = (await session.execute(select(Alert))).scalars().all()

    # normalised alert messages once; matching is containment inside a window
    haystacks = [(a, _norm(a.message or "")) for a in alerts]
    for bet in bets:
        needle = _norm(bet.selection)
        if len(needle) < 4:
            continue  # "over" / "u21" style needles match everything — skip
        for alert, hay in haystacks:
            if needle not in hay:
                continue
            if bet.placed_at is not None:
                created = (alert.created_at if alert.created_at.tzinfo
                           else alert.created_at.replace(tzinfo=dt.UTC))
                if not (created - _MATCH_AFTER <= bet.placed_at
                        <= created + _MATCH_BEFORE):
                    continue
            bet.alert_id = str(alert.id)
            bet.alert_kind = alert.kind
            quote, kelly = _alert_quote(alert.kind, alert.payload or {})
            bet.alert_odds = quote
            bet.alert_kelly = kelly
            break

    def _bucket() -> dict[str, Any]:
        return {"bets": 0, "settled": 0, "wins": 0, "staked": 0.0, "pnl": 0.0}

    by_kind: dict[str, dict[str, Any]] = {}
    own = _bucket()
    price_diffs: list[float] = []       # your odds vs the alert's quoted odds (%)
    stake_ratios: list[float] = []      # your stake / printed kelly
    for bet in bets:
        bucket = by_kind.setdefault(bet.alert_kind, _bucket()) if bet.alert_kind else own
        bucket["bets"] += 1
        if bet.result in ("win", "loss") and bet.pnl is not None and bet.stake:
            bucket["settled"] += 1
            bucket["staked"] += bet.stake
            bucket["pnl"] += bet.pnl
            if bet.result == "win":
                bucket["wins"] += 1
        if (bet.alert_odds and bet.alert_odds > 1.0
                and bet.odds and bet.odds > 1.0):
            price_diffs.append((bet.odds / bet.alert_odds - 1.0) * 100.0)
        if bet.alert_kelly and bet.alert_kelly > 0 and bet.stake:
            stake_ratios.append(bet.stake / bet.alert_kelly)

    for bucket in [*by_kind.values(), own]:
        bucket["staked"] = round(bucket["staked"], 2)
        bucket["pnl"] = round(bucket["pnl"], 2)
        bucket["roi_pct"] = (round(bucket["pnl"] / bucket["staked"] * 100.0, 1)
                             if bucket["staked"] else None)

    # counterfactual: the scoreboard's verdict on EVERY alert in the same
    # window, next to what you actually did
    counterfactual: dict[str, Any] | None = None
    if stamped_times:
        from sportsdata_agents.quant.scoreboard import alert_pnl

        counterfactual = await alert_pnl(
            session,
            since=min(stamped_times) - _MATCH_BEFORE,
            until=max(stamped_times) + _MATCH_AFTER,
        )

    matched = sum(1 for b in bets if b.alert_id)
    return {
        "bets": len(bets),
        "matched_to_alerts": matched,
        "own_picks": len(bets) - matched,
        "by_kind": dict(sorted(by_kind.items())),
        "own": own,
        "price_vs_alert": {
            "n": len(price_diffs),
            "mean_pct": round(sum(price_diffs) / len(price_diffs), 2)
            if price_diffs else None,
            "beat_quote_share": round(
                sum(1 for d in price_diffs if d > 0) / len(price_diffs), 2)
            if price_diffs else None,
        },
        "stake_vs_kelly": {
            "n": len(stake_ratios),
            "mean_ratio": round(sum(stake_ratios) / len(stake_ratios), 2)
            if stake_ratios else None,
        },
        "counterfactual": counterfactual,
        "unmatched_rows": [b.row for b in bets if not b.alert_id][:50],
    }


def format_shadow_report(report: dict[str, Any]) -> str:
    lines = [":ledger: Shadow account — your bets vs the alert stream"]
    lines.append(f"{report['bets']} bets in journal · "
                 f"{report['matched_to_alerts']} matched to alerts · "
                 f"{report['own_picks']} own picks")
    for kind, b in (report.get("by_kind") or {}).items():
        roi = f" ({b['roi_pct']:+.1f}% ROI)" if b["roi_pct"] is not None else ""
        lines.append(f"  {kind}: {b['bets']} bets, {b['settled']} settled, "
                     f"{b['wins']} won · staked ${b['staked']:.2f} · "
                     f"P&L ${b['pnl']:+.2f}{roi}")
    own = report.get("own") or {}
    if own.get("bets"):
        roi = f" ({own['roi_pct']:+.1f}% ROI)" if own.get("roi_pct") is not None else ""
        lines.append(f"  own picks: {own['bets']} bets, {own['settled']} settled, "
                     f"{own['wins']} won · staked ${own['staked']:.2f} · "
                     f"P&L ${own['pnl']:+.2f}{roi}")
    price = report.get("price_vs_alert") or {}
    if price.get("n"):
        lines.append(f"Price vs alert quote: mean {price['mean_pct']:+.2f}% over "
                     f"{price['n']} matched bets · beat the quote "
                     f"{price['beat_quote_share']:.0%} of the time")
    kelly = report.get("stake_vs_kelly") or {}
    if kelly.get("n"):
        lines.append(f"Stake vs printed Kelly: mean {kelly['mean_ratio']:.2f}x "
                     f"over {kelly['n']} matched bets")
    if report.get("counterfactual"):
        lines.append("Counterfactual (every alert taken, scoreboard rules) "
                     "rides below:")
    return "\n".join(lines)
