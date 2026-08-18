from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fast_zonebourse_scan_v2 import PARIS
from sync_zonebourse_browser import synchronize_with_browser


LISTING = """
<a href="/actualite-bourse/achat-du-turbo-call-vontobel-mu13v-ce123abc">
RUSSELL 2000 TURBO - MU13V - 07/08 Achat du turbo CALL Vontobel MU13V
Prix d'exercice Barrière Maturité Elasticité 2 642,36 € 2 642,36 € Illimité 7.78x CALL
</a>
"""

ARTICLE = """
<html><body>
<div>ISIN DE000MU13V01</div>
<div>Cours d'entrée : 1,20 €</div>
<div>Objectif de cours : 2 800,00 USD</div>
<div>Seuil d'invalidation sous 2 600,00 USD</div>
</body></html>
"""

FALLBACK_LISTING = """
[SOITEC WARRANT - B9W5B - 14/08 Un soutien à mettre à profit Prix d'exercice Maturité Elasticité Delta 165,00 € 18/09/2026 5.02x 0.336 CALL](https://www.zonebourse.com/actualite-bourse/un-soutien-a-mettre-a-profit-ce7859ded08ff624)
"""


class ZonebourseBrowserSyncTests(unittest.TestCase):
    def test_browser_render_enriches_russell_position(self):
        document = {"meta": {}, "positions": [{
            "id": "russell", "asset": "Russell 2000", "productType": "Turbo",
            "direction": "CALL", "mnemo": "MU13V", "status": "open",
            "entryPrice": None, "isin": None, "target": None, "stop": None,
            "url": "https://www.zonebourse.com/actualite-bourse/achat-du-turbo-call-vontobel-mu13v-ce123abc",
        }]}

        def requester(url, **_kwargs):
            if "/cloturees/" in url:
                return "<html></html>"
            if "/actualite-bourse/" in url:
                return ARTICLE
            return LISTING

        result = synchronize_with_browser(
            document, requester, datetime(2026, 8, 15, 12, tzinfo=PARIS)
        )
        position = document["positions"][0]
        self.assertFalse(result["degraded"])
        self.assertEqual(position["isin"], "DE000MU13V01")
        self.assertEqual(position["entryPrice"], 1.2)
        self.assertEqual(position["target"], 2800)
        self.assertEqual(position["stop"], 2600)
        self.assertGreater(position["confidence"]["score"], 50)

    def test_uses_text_fallback_when_browser_listing_has_no_cards(self):
        document = {"meta": {}, "positions": []}

        def requester(url, **_kwargs):
            if "/cloturees/" in url:
                return "<html></html>"
            if "r.jina.ai/http://www.zonebourse.com/bourse/derives" in url:
                return f"<html><body><pre>{FALLBACK_LISTING}</pre></body></html>"
            if "r.jina.ai/http://www.zonebourse.com/actualite-bourse/" in url:
                return ARTICLE
            if "/actualite-bourse/" in url:
                return "<html><body>page filtrée</body></html>"
            return "<html><body>page filtrée</body></html>"

        result = synchronize_with_browser(
            document,
            requester,
            datetime(2026, 8, 18, 12, tzinfo=PARIS),
            text_requester=requester,
        )

        self.assertFalse(result["degraded"])
        self.assertEqual(result["sourceUsed"], "jina-zonebourse-fr")
        self.assertEqual(result["openSeen"], 1)
        self.assertEqual(document["positions"][0]["mnemo"], "B9W5B")
        self.assertEqual(document["positions"][0]["isin"], "DE000MU13V01")
        self.assertEqual(document["positions"][0]["entryPrice"], 1.2)


if __name__ == "__main__":
    unittest.main()
