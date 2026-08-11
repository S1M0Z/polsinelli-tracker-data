#!/usr/bin/env python3
"""Build a conservative investment decision view from tracked positions.

The engine uses only the Python standard library. It never creates a buy signal
when essential market data is stale or missing.
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class DataError(ValueError):
    """Raised when source data is invalid or internally inconsistent."""


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DataError(f"Invalid ISO-8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise DataError(f"Datetime must include a timezone: {value}")
    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pct_change(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0:
        return None
    return ((end / start) - 1.0) * 100.0


def round_or_none(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataError(f"Expected a JSON object in {path}")
    return value


def validate_positions(document: dict[str, Any]) -> list[dict[str, Any]]:
    positions = document.get("positions")
    if not isinstance(positions, list):
        raise DataError("positions.json must contain a positions array")

    seen_ids: set[str] = set()
    allowed_status = {"open", "closed"}
    allowed_direction = {"CALL", "PUT"}
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise DataError(f"Position #{index} is not an object")
        position_id = position.get("id")
        if not isinstance(position_id, str) or not position_id:
            raise DataError(f"Position #{index} has no valid id")
        if position_id in seen_ids:
            raise DataError(f"Duplicate position id: {position_id}")
        seen_ids.add(position_id)
        if position.get("status") not in allowed_status:
            raise DataError(f"Invalid status for {position_id}")
        if position.get("direction") not in allowed_direction:
            raise DataError(f"Invalid direction for {position_id}")
        entry = position.get("entryPrice")
        if position["status"] == "closed" and (
            not isinstance(entry, (int, float)) or entry <= 0
        ):
            raise DataError(f"Invalid entryPrice for closed position {position_id}")
        if entry is not None and (
            not isinstance(entry, (int, float)) or isinstance(entry, bool) or entry <= 0
        ):
            raise DataError(f"Invalid entryPrice for {position_id}")
        if position["status"] == "closed":
            exit_price = position.get("exitPrice")
            if not isinstance(exit_price, (int, float)) or exit_price < 0:
                raise DataError(f"Closed position {position_id} needs exitPrice")
            if not position.get("exitDate"):
                raise DataError(f"Closed position {position_id} needs exitDate")
    return positions


def latest_quotes(quotes_document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshots = quotes_document.get("quotes", [])
    if not isinstance(snapshots, list):
        raise DataError("quote-history.json must contain a quotes array")
    latest: dict[str, dict[str, Any]] = {}
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        position_id = item.get("positionId")
        quote_at = parse_datetime(item.get("quoteAt"))
        if not isinstance(position_id, str) or quote_at is None:
            continue
        current = latest.get(position_id)
        current_time = parse_datetime(current.get("quoteAt")) if current else None
        if current_time is None or quote_at > current_time:
            latest[position_id] = item
    return latest


@dataclass(frozen=True)
class Policy:
    model_capital_eur: float
    fixed_stake_eur: float
    max_quote_age_minutes: int
    max_price_drift_pct: float
    max_spread_pct: float
    min_reward_risk_ratio: float
    max_detection_latency_seconds: int
    max_risk_per_trade_pct: float
    max_total_leveraged_exposure_pct: float
    max_open_positions: int
    assume_full_premium_at_risk: bool

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Policy":
        raw = document.get("policy", document)
        required = {
            "modelCapitalEur",
            "fixedStakeEur",
            "maxQuoteAgeMinutes",
            "maxPriceDriftPct",
            "maxSpreadPct",
            "minRewardRiskRatio",
            "maxDetectionLatencySeconds",
            "maxRiskPerTradePct",
            "maxTotalLeveragedExposurePct",
            "maxOpenPositions",
            "assumeFullPremiumAtRisk",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise DataError(f"Missing policy fields: {', '.join(missing)}")
        return cls(
            model_capital_eur=float(raw["modelCapitalEur"]),
            fixed_stake_eur=float(raw["fixedStakeEur"]),
            max_quote_age_minutes=int(raw["maxQuoteAgeMinutes"]),
            max_price_drift_pct=float(raw["maxPriceDriftPct"]),
            max_spread_pct=float(raw["maxSpreadPct"]),
            min_reward_risk_ratio=float(raw["minRewardRiskRatio"]),
            max_detection_latency_seconds=int(raw["maxDetectionLatencySeconds"]),
            max_risk_per_trade_pct=float(raw["maxRiskPerTradePct"]),
            max_total_leveraged_exposure_pct=float(raw["maxTotalLeveragedExposurePct"]),
            max_open_positions=int(raw["maxOpenPositions"]),
            assume_full_premium_at_risk=bool(raw["assumeFullPremiumAtRisk"]),
        )


def merged_market_data(position: dict[str, Any], quote: dict[str, Any] | None) -> dict[str, Any]:
    data = {
        "currentPrice": position.get("currentPrice"),
        "quoteAt": position.get("quoteAt"),
        "bid": position.get("bid"),
        "ask": position.get("ask"),
        "underlyingPrice": position.get("underlyingPrice"),
        "publicationPrice": position.get("publicationPrice", position.get("entryPrice")),
        "detectedAt": position.get("detectedAt"),
        "publishedAt": position.get("publishedAt"),
    }
    if quote:
        for key in data:
            if quote.get(key) is not None:
                data[key] = quote[key]
        if quote.get("price") is not None:
            data["currentPrice"] = quote["price"]
    return data


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    return ((ask - bid) / midpoint) * 100.0


def entry_decision(
    position: dict[str, Any], market: dict[str, Any], policy: Policy, as_of: datetime
) -> tuple[str, list[str], dict[str, Any]]:
    if position["status"] == "closed":
        return "CLOSED", [], {}

    reasons: list[str] = []
    quote_at = parse_datetime(market.get("quoteAt"))
    quote_age_minutes: float | None = None
    if quote_at is None:
        reasons.append("missing_quote_timestamp")
    else:
        quote_age_minutes = max(0.0, (as_of - quote_at).total_seconds() / 60.0)
        if quote_age_minutes > policy.max_quote_age_minutes:
            reasons.append("stale_quote")

    current_price = market.get("currentPrice")
    if not isinstance(current_price, (int, float)) or current_price <= 0:
        reasons.append("missing_current_price")

    bid = market.get("bid")
    ask = market.get("ask")
    current_spread = spread_pct(
        float(bid) if isinstance(bid, (int, float)) else None,
        float(ask) if isinstance(ask, (int, float)) else None,
    )
    if current_spread is None:
        reasons.append("missing_bid_ask")
    elif current_spread > policy.max_spread_pct:
        reasons.append("spread_too_wide")

    publication_price = market.get("publicationPrice")
    executable_price = ask if isinstance(ask, (int, float)) and ask > 0 else current_price
    drift = pct_change(
        float(publication_price) if isinstance(publication_price, (int, float)) else None,
        float(executable_price) if isinstance(executable_price, (int, float)) else None,
    )
    if drift is None:
        reasons.append("missing_publication_price")
    elif drift > policy.max_price_drift_pct:
        reasons.append("price_drift_too_high")

    published_at = parse_datetime(market.get("publishedAt"))
    detected_at = parse_datetime(market.get("detectedAt"))
    detection_latency_seconds: float | None = None
    if published_at is None or detected_at is None:
        reasons.append("missing_detection_timestamps")
    else:
        detection_latency_seconds = max(0.0, (detected_at - published_at).total_seconds())
        if detection_latency_seconds > policy.max_detection_latency_seconds:
            reasons.append("detection_too_slow")

    target = position.get("target")
    stop = position.get("stop")
    underlying = market.get("underlyingPrice")
    reward_risk_ratio: float | None = None
    if not all(isinstance(value, (int, float)) for value in (target, stop, underlying)):
        reasons.append("missing_risk_levels")
    else:
        target_value = float(target)
        stop_value = float(stop)
        underlying_value = float(underlying)
        if position.get("direction") == "CALL":
            reward = target_value - underlying_value
            risk = underlying_value - stop_value
        else:
            reward = underlying_value - target_value
            risk = stop_value - underlying_value
        if reward <= 0 or risk <= 0:
            reasons.append("invalid_reward_risk")
        else:
            reward_risk_ratio = reward / risk
            if reward_risk_ratio < policy.min_reward_risk_ratio:
                reasons.append("reward_risk_too_low")

    if "stale_quote" in reasons or "missing_quote_timestamp" in reasons:
        decision = "STALE_QUOTE"
    elif any(reason.startswith("missing_") for reason in reasons):
        decision = "DATA_INCOMPLETE"
    elif "spread_too_wide" in reasons:
        decision = "SPREAD_TOO_WIDE"
    elif "price_drift_too_high" in reasons or "detection_too_slow" in reasons:
        decision = "TOO_LATE"
    elif "reward_risk_too_low" in reasons or "invalid_reward_risk" in reasons:
        decision = "RISK_REWARD_REJECTED"
    else:
        decision = "ELIGIBLE"

    diagnostics = {
        "quoteAgeMinutes": round_or_none(quote_age_minutes, 2),
        "spreadPct": round_or_none(current_spread, 4),
        "priceDriftPct": round_or_none(drift, 4),
        "detectionLatencySeconds": round_or_none(detection_latency_seconds, 2),
        "rewardRiskRatio": round_or_none(reward_risk_ratio, 4),
    }
    return decision, reasons, diagnostics


def position_stake_limit(policy: Policy) -> float:
    risk_limit = policy.model_capital_eur * policy.max_risk_per_trade_pct / 100.0
    exposure_limit = (
        policy.model_capital_eur * policy.max_total_leveraged_exposure_pct / 100.0
    ) / max(1, policy.max_open_positions)
    if policy.assume_full_premium_at_risk:
        return min(risk_limit, exposure_limit)
    return exposure_limit


def closed_statistics(closed: Iterable[dict[str, Any]], fixed_stake: float) -> dict[str, Any]:
    returns: list[float] = []
    pnl_values: list[float] = []
    rows: list[dict[str, Any]] = []
    for position in closed:
        ret = pct_change(float(position["entryPrice"]), float(position["exitPrice"]))
        if ret is None:
            continue
        pnl = fixed_stake * ret / 100.0
        returns.append(ret)
        pnl_values.append(pnl)
        rows.append(
            {
                "positionId": position["id"],
                "asset": position.get("asset"),
                "returnPct": round(ret, 4),
                "fixedStakePnlEur": round(pnl, 2),
                "entryDate": position.get("entryDate"),
                "exitDate": position.get("exitDate"),
            }
        )

    winners = [value for value in returns if value > 0]
    losers = [value for value in returns if value <= 0]
    positive_pnl = sum(value for value in pnl_values if value > 0)
    negative_pnl = abs(sum(value for value in pnl_values if value < 0))
    return {
        "tradeCount": len(returns),
        "winCount": len(winners),
        "lossCount": len(losers),
        "winRatePct": round(100 * len(winners) / len(returns), 2) if returns else None,
        "averageReturnPct": round(statistics.fmean(returns), 4) if returns else None,
        "medianReturnPct": round(statistics.median(returns), 4) if returns else None,
        "bestReturnPct": round(max(returns), 4) if returns else None,
        "worstReturnPct": round(min(returns), 4) if returns else None,
        "fixedStakeEur": fixed_stake,
        "fixedStakeTotalPnlEur": round(sum(pnl_values), 2),
        "profitFactor": round(positive_pnl / negative_pnl, 4) if negative_pnl > 0 else None,
        "trades": sorted(rows, key=lambda row: (row["exitDate"] or "", row["positionId"])),
        "limitations": [
            "Returns use stated entry and exit prices, not necessarily executable prices.",
            "Fixed-stake results exclude spread, fees, slippage and overlapping capital usage.",
        ],
    }


def build_view(
    positions_document: dict[str, Any],
    policy_document: dict[str, Any],
    quotes_document: dict[str, Any],
    as_of: datetime,
) -> dict[str, Any]:
    positions = validate_positions(positions_document)
    policy = Policy.from_document(policy_document)
    quote_map = latest_quotes(quotes_document)
    open_positions = [p for p in positions if p["status"] == "open"]
    closed_positions = [p for p in positions if p["status"] == "closed"]

    stake_limit = position_stake_limit(policy)
    open_rows: list[dict[str, Any]] = []
    decision_counts: dict[str, int] = {}

    for position in open_positions:
        market = merged_market_data(position, quote_map.get(position["id"]))
        decision, reasons, diagnostics = entry_decision(position, market, policy, as_of)
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        entry_price = position.get("entryPrice")
        mark_return = pct_change(
            float(entry_price) if isinstance(entry_price, (int, float)) else None,
            float(market["currentPrice"])
            if isinstance(market.get("currentPrice"), (int, float))
            else None,
        )
        open_rows.append(
            {
                "positionId": position["id"],
                "asset": position.get("asset"),
                "productType": position.get("productType"),
                "direction": position.get("direction"),
                "mnemo": position.get("mnemo"),
                "entryPrice": position.get("entryPrice"),
                "market": market,
                "markToMarketReturnPct": round_or_none(mark_return, 4),
                "decision": decision,
                "reasons": reasons,
                "diagnostics": diagnostics,
                "maxModelStakeEur": round(stake_limit, 2),
                "sourceUrl": position.get("url"),
            }
        )

    actionable = [row for row in open_rows if row["decision"] == "ELIGIBLE"]
    if len(actionable) > policy.max_open_positions:
        for row in actionable[policy.max_open_positions :]:
            row["decision"] = "PORTFOLIO_LIMIT"
            row["reasons"].append("max_open_positions_reached")
        decision_counts["ELIGIBLE"] = policy.max_open_positions
        decision_counts["PORTFOLIO_LIMIT"] = len(actionable) - policy.max_open_positions

    return {
        "meta": {
            "generatedAt": as_of.isoformat(),
            "engineVersion": "1.0.0",
            "sourceGeneratedAt": positions_document.get("meta", {}).get("generatedAt"),
            "purpose": "Conservative decision support; not an automated order system.",
        },
        "policy": {
            "modelCapitalEur": policy.model_capital_eur,
            "fixedStakeEur": policy.fixed_stake_eur,
            "maxQuoteAgeMinutes": policy.max_quote_age_minutes,
            "maxPriceDriftPct": policy.max_price_drift_pct,
            "maxSpreadPct": policy.max_spread_pct,
            "minRewardRiskRatio": policy.min_reward_risk_ratio,
            "maxDetectionLatencySeconds": policy.max_detection_latency_seconds,
            "maxRiskPerTradePct": policy.max_risk_per_trade_pct,
            "maxTotalLeveragedExposurePct": policy.max_total_leveraged_exposure_pct,
            "maxOpenPositions": policy.max_open_positions,
            "assumeFullPremiumAtRisk": policy.assume_full_premium_at_risk,
            "maxModelStakePerPositionEur": round(stake_limit, 2),
        },
        "summary": {
            "positionCount": len(positions),
            "openCount": len(open_positions),
            "closedCount": len(closed_positions),
            "actionableCount": sum(1 for row in open_rows if row["decision"] == "ELIGIBLE"),
            "decisionCounts": decision_counts,
            "systemVerdict": "NO_ACTIONABLE_POSITION"
            if not any(row["decision"] == "ELIGIBLE" for row in open_rows)
            else "ACTIONABLE_POSITIONS_AVAILABLE",
        },
        "openPositions": open_rows,
        "closedTrackRecord": closed_statistics(closed_positions, policy.fixed_stake_eur),
        "guardrails": [
            "Never enter from a stale quote.",
            "Never enter without a current bid/ask spread.",
            "Reject a signal when the executable price has drifted too far from publication.",
            "Treat the full premium as at risk for leveraged products unless proven otherwise.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positions", type=Path, default=Path("positions.json"))
    parser.add_argument("--policy", type=Path, default=Path("risk-policy.json"))
    parser.add_argument("--quotes", type=Path, default=Path("quote-history.json"))
    parser.add_argument("--output", type=Path, default=Path("investment-view.json"))
    parser.add_argument("--as-of", help="ISO-8601 generation timestamp; defaults to now")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the generated output differs from the existing output file",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = parse_datetime(args.as_of) if args.as_of else utc_now()
    assert as_of is not None
    view = build_view(
        read_json(args.positions),
        read_json(args.policy),
        read_json(args.quotes),
        as_of,
    )
    serialized = json.dumps(view, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        existing = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if existing != serialized:
            raise SystemExit(f"{args.output} is stale; rebuild it with investment_engine.py")
    else:
        args.output.write_text(serialized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
