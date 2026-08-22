from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from investment_engine import Policy, build_view, closed_statistics, entry_decision, position_stake_limit

POLICY = {
    "policy": {
        "modelCapitalEur": 10000,
        "fixedStakeEur": 100,
        "maxQuoteAgeMinutes": 30,
        "maxWatchAgeMinutes": 30,
        "maxPositionsAgeMinutes": 60,
        "maxPriceDriftPct": 5,
        "maxSpreadPct": 4,
        "minRewardRiskRatio": 1.5,
        "maxDetectionLatencySeconds": 300,
        "maxRiskPerTradePct": 0.5,
        "maxTotalLeveragedExposurePct": 5,
        "maxOpenPositions": 3,
        "assumeFullPremiumAtRisk": True,
    }
}


class InvestmentEngineTests(unittest.TestCase):
    def test_open_recommendation_can_wait_for_confirmed_entry_price(self):
        document = {"positions": [{**self.position, "entryPrice": None}]}
        view = build_view(document, POLICY, {"quotes": []}, self.as_of)
        self.assertIn("missing_publication_price", view["openPositions"][0]["reasons"])

    def setUp(self) -> None:
        self.as_of = datetime.fromisoformat("2026-07-30T13:52:00+02:00")
        self.policy = Policy.from_document(POLICY)
        self.position = {
            "id": "demo-1",
            "asset": "Demo",
            "productType": "Turbo",
            "direction": "CALL",
            "status": "open",
            "entryPrice": 1.0,
            "entryDate": "2026-07-30",
            "target": 120,
            "stop": 100,
            "riskLevelsCurrency": "EUR",
        }

    def market(self, **overrides):
        value = {
            "currentPrice": 1.02,
            "quoteAt": "2026-07-30T13:45:00+02:00",
            "bid": 1.01,
            "ask": 1.03,
            "publicationPrice": 1.0,
            "underlyingPrice": 108,
            "underlyingQuoteAt": "2026-07-30T13:45:00+02:00",
            "quoteCurrency": "EUR",
            "underlyingCurrency": "EUR",
            "publishedAt": "2026-07-30T13:40:00+02:00",
            "detectedAt": "2026-07-30T13:42:00+02:00",
            "timestampSource": "provider_market_time",
            "marketStatus": "open",
            "isIndicative": False,
        }
        value.update(overrides)
        return value

    def test_stale_quote_is_blocked(self) -> None:
        decision, reasons, _ = entry_decision(
            self.position,
            self.market(quoteAt="2026-07-30T12:00:00+02:00"),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "STALE_QUOTE")
        self.assertIn("stale_quote", reasons)

    def test_missing_bid_ask_is_blocked(self) -> None:
        market = self.market()
        market.pop("bid")
        market.pop("ask")
        decision, reasons, _ = entry_decision(self.position, market, self.policy, self.as_of)
        self.assertEqual(decision, "DATA_INCOMPLETE")
        self.assertIn("missing_bid_ask", reasons)

    def test_wide_spread_is_blocked(self) -> None:
        decision, reasons, diagnostics = entry_decision(
            self.position,
            self.market(bid=0.98, ask=1.06),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "SPREAD_TOO_WIDE")
        self.assertIn("spread_too_wide", reasons)
        self.assertGreater(diagnostics["spreadPct"], 4)

    def test_fresh_tight_quote_is_eligible(self) -> None:
        decision, reasons, _ = entry_decision(
            self.position, self.market(), self.policy, self.as_of
        )
        self.assertEqual(decision, "ELIGIBLE")
        self.assertEqual(reasons, [])

    def test_full_premium_risk_caps_stake_at_fifty_euros(self) -> None:
        self.assertEqual(position_stake_limit(self.policy), 50)

    def test_future_quote_is_rejected(self) -> None:
        decision, reasons, diagnostics = entry_decision(
            self.position,
            self.market(quoteAt="2026-07-30T14:00:00+02:00"),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "INVALID_TIMESTAMPS")
        self.assertIn("quote_timestamp_in_future", reasons)
        self.assertLess(diagnostics["quoteAgeMinutes"], 0)

    def test_detection_before_publication_is_rejected(self) -> None:
        decision, reasons, _ = entry_decision(
            self.position,
            self.market(detectedAt="2026-07-30T13:39:00+02:00"),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "INVALID_TIMESTAMPS")
        self.assertIn("detection_before_publication", reasons)

    def test_date_only_publication_does_not_invent_intraday_latency(self) -> None:
        decision, reasons, diagnostics = entry_decision(
            self.position,
            self.market(publishedAtPrecision="date"),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "DATA_INCOMPLETE")
        self.assertIn("missing_publication_time", reasons)
        self.assertIsNone(diagnostics["detectionLatencySeconds"])

    def test_only_executed_holdings_consume_portfolio_slots(self) -> None:
        positions = []
        quotes = []
        for index in range(4):
            position = {
                **self.position,
                "id": f"demo-{index}",
                "portfolioStatus": "held",
                "executedPrice": 1.0,
                "quantity": 10,
            }
            positions.append(position)
            quotes.append({"positionId": position["id"], **self.market(), "price": 1.02})
        view = build_view({"positions": positions}, POLICY, {"quotes": quotes}, self.as_of)
        self.assertEqual(view["summary"]["committedOpenCount"], 4)
        self.assertEqual(view["summary"]["availablePositionSlots"], 0)
        self.assertEqual(view["summary"]["actionableCount"], 0)
        self.assertEqual(view["summary"]["decisionCounts"]["PORTFOLIO_LIMIT"], 4)

    def test_closed_statistics(self) -> None:
        stats = closed_statistics(
            [
                {"id": "a", "asset": "A", "entryPrice": 1, "exitPrice": 1.2},
                {"id": "b", "asset": "B", "entryPrice": 1, "exitPrice": 0.5},
            ],
            100,
        )
        self.assertEqual(stats["tradeCount"], 2)
        self.assertEqual(stats["fixedStakeTotalPnlEur"], -30)

    def test_build_view_blocks_missing_quotes(self) -> None:
        document = {"meta": {}, "positions": [self.position]}
        view = build_view(document, POLICY, {"quotes": []}, self.as_of)
        self.assertEqual(view["summary"]["actionableCount"], 0)
        self.assertEqual(view["summary"]["systemVerdict"], "DATA_PIPELINE_UNAVAILABLE")

    def test_recommendation_entry_price_is_not_a_portfolio_holding(self) -> None:
        view = build_view({"positions": [self.position]}, POLICY, {"quotes": []}, self.as_of)
        self.assertEqual(view["summary"]["committedOpenCount"], 0)

    def test_policy_rejects_string_boolean(self) -> None:
        invalid = {"policy": {**POLICY["policy"], "assumeFullPremiumAtRisk": "false"}}
        with self.assertRaisesRegex(Exception, "JSON boolean"):
            Policy.from_document(invalid)

    def test_unconfirmed_quote_timestamp_is_blocked(self) -> None:
        decision, reasons, _ = entry_decision(
            self.position,
            self.market(timestampSource="unknown", isIndicative=True),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "DATA_INCOMPLETE")
        self.assertIn("unconfirmed_market_timestamp", reasons)

    def test_mixed_underlying_session_is_blocked(self) -> None:
        decision, reasons, _ = entry_decision(
            self.position,
            self.market(underlyingQuoteAt="2026-07-30T13:30:00+02:00"),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "DATA_INCOMPLETE")
        self.assertIn("mixed_market_sessions", reasons)

    def test_closed_market_is_blocked(self) -> None:
        decision, reasons, _ = entry_decision(
            self.position, self.market(marketStatus="closed"), self.policy, self.as_of
        )
        self.assertEqual(decision, "DATA_INCOMPLETE")
        self.assertIn("market_not_confirmed_open", reasons)

    def test_product_and_underlying_may_use_different_currencies(self) -> None:
        position = {**self.position, "riskLevelsCurrency": "USD"}
        decision, reasons, _ = entry_decision(
            position,
            self.market(quoteCurrency="EUR", underlyingCurrency="USD"),
            self.policy,
            self.as_of,
        )
        self.assertEqual(decision, "ELIGIBLE")
        self.assertNotIn("currency_mismatch", reasons)

    def test_stale_watch_blocks_otherwise_actionable_positions(self) -> None:
        quote = {"positionId": self.position["id"], **self.market(), "price": 1.02}
        document = {"meta": {
            "generatedAt": "2026-07-30T10:00:00+02:00",
            "lastPublicationCheckedAt": "2026-07-30T10:00:00+02:00",
        }, "positions": [self.position]}
        view = build_view(document, POLICY, {"quotes": [quote]}, self.as_of)
        self.assertEqual(view["openPositions"][0]["decision"], "ELIGIBLE")
        self.assertEqual(view["summary"]["systemVerdict"], "SYSTEM_STALE")


if __name__ == "__main__":
    unittest.main()
