import tempfile
import unittest
from pathlib import Path

from scripts.fast_zonebourse_scan import classify, parse_articles


FIXTURE = """
<html><body>
<a href="/actualite-bourse/correction-technique-en-vue-ce7f51dfda8df125">
BANK VONTOBEL/PUT/TOTALENERGIES/72/0.2/19.03.27: Correction technique en vue Le 24 juillet 2026 à 11:39
</a>
<a href="/actualite-bourse/l-europe-reprend-un-peu-de-couleurs-ce7f51dfda8af42c">
CAC 40: L'Europe reprend un peu de couleurs Le 24 juillet 2026 à 11:23
</a>
</body></html>
"""


class FastZonebourseScanTests(unittest.TestCase):
    def test_parse_articles_extracts_title_date_and_kind(self):
        articles = parse_articles(FIXTURE)
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0].kind, "position_candidate")
        self.assertEqual(articles[0].publishedAt, "2026-07-24T11:39+02:00")
        self.assertEqual(articles[1].kind, "market_analysis")

    def test_classify_market_and_product_articles(self):
        self.assertEqual(classify("S&P 500: En hausse"), "market_analysis")
        self.assertEqual(
            classify("BNP PARIBAS/CALL/LEGRAND: Achat du warrant CALL"),
            "position_candidate",
        )
        self.assertEqual(classify("Article sans catégorie"), "other")


if __name__ == "__main__":
    unittest.main()
