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
        }

    def market(self, **overrides):
        value = {
            "currentPrice": 1.02,
            "quoteAt": "2026-07-30T13:45:00+02:00",
            "bid": 1.01,
            "ask": 1.03,
            "publicationPrice": 1.0,
            "underlyingPrice": 108,
            "publishedAt": "2026-07-30T13:40:00+02:00",
            "detectedAt": "2026-07-30T13:42:00+02:00",
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
        self.assertEqual(view["summary"]["systemVerdict"], "NO_ACTIONABLE_POSITION")


if __name__ == "__main__":
    unittest.main()
