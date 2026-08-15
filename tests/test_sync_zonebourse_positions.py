import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.sync_zonebourse_positions import parse_closed_cards, synchronize

PARIS = ZoneInfo("Europe/Paris")


class ZoneboursePositionSyncTests(unittest.TestCase):
    def test_parses_explicit_exit_prices(self):
        html = '''<a href="/actualite-bourse/prises-de-benefices-ce7f50dcd98ff526">
        VINCI WARRANT - WC25V - 05/08 Prises de bénéfices (+27.68%)
        Prix d'exercice Entrée Sortie Performance 120,00 € 1,120 1,430 +27,68 % SORTIE</a>'''
        rows = parse_closed_cards(
            html, "https://www.zonebourse.com/", datetime(2026, 8, 10, tzinfo=PARIS)
        )
        self.assertEqual(rows[0]["entryPrice"], 1.12)
        self.assertEqual(rows[0]["exitPrice"], 1.43)
        self.assertEqual(rows[0]["realizedReturnPct"], 27.68)

    def test_adds_new_card_and_closes_only_from_explicit_exit(self):
        document = {"meta": {}, "positions": []}
        current = [{
            "url": "https://www.zonebourse.com/actualite-bourse/test-ce123abc",
            "title": "Achat du turbo CALL",
            "publishedAt": "2026-08-10T11:12+02:00",
            "underlying": "STMICROELECTRONICS N.V.",
            "productType": "WARRANT",
            "productCode": "OJ23V",
            "publishedAtPrecision": "minute",
        }]
        result = synchronize(document, current, [], datetime(2026, 8, 10, 12, tzinfo=PARIS))
        self.assertEqual(result["added"], 1)
        self.assertEqual(document["positions"][0]["mnemo"], "OJ23V")
        self.assertIsNone(document["positions"][0]["entryPrice"])
        self.assertEqual(document["positions"][0]["status"], "open")

    def test_unchanged_position_does_not_force_a_rewrite(self):
        current = [{
            "url": "https://www.zonebourse.com/actualite-bourse/test-ce123abc",
            "title": "Achat du turbo CALL",
            "publishedAt": "2026-08-10T11:12+02:00",
            "underlying": "STMICROELECTRONICS N.V.",
            "productType": "WARRANT",
            "productCode": "OJ23V",
        }]
        document = {
            "meta": {"automaticSync": True, "openRecommendationCount": 1},
            "positions": [{
                "mnemo": "OJ23V", "status": "open", "url": current[0]["url"],
                "asset": current[0]["underlying"], "productType": "Warrant",
                "publishedAt": current[0]["publishedAt"],
                "publishedAtPrecision": "minute",
                "detectedAt": "2026-08-10T11:13:00+02:00",
            }],
        }
        result = synchronize(document, current, [], datetime(2026, 8, 10, 12, tzinfo=PARIS))
        self.assertFalse(result["changed"])

    def test_backfills_null_publication_and_detection_timestamps(self):
        current = [{
            "url": "https://www.zonebourse.com/actualite-bourse/test-ce123abc",
            "title": "Achat du warrant PUT",
            "publishedAt": "2026-08-10T11:12+02:00",
            "underlying": "STMICROELECTRONICS N.V.",
            "productType": "WARRANT",
            "productCode": "OJ23V",
            "direction": "PUT",
        }]
        document = {"meta": {}, "positions": [{
            "id": "legacy", "mnemo": "OJ23V", "status": "open",
            "url": current[0]["url"], "publishedAt": None, "detectedAt": None,
        }]}
        result = synchronize(document, current, [], datetime(2026, 8, 10, 12, tzinfo=PARIS))
        position = document["positions"][0]
        self.assertTrue(result["changed"])
        self.assertEqual(position["publishedAt"], current[0]["publishedAt"])
        self.assertEqual(position["detectedAt"], "2026-08-10T12:00:00+02:00")
        self.assertEqual(position["direction"], "PUT")


if __name__ == "__main__":
    unittest.main()
