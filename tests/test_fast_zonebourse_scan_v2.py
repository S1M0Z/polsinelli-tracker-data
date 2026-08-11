import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.fast_zonebourse_scan_v2 import (
    canonical_article_url,
    parse_html_articles,
    parse_markdown_articles,
    parse_recommendation,
)

HTML_FIXTURE = """
<html><body>
<a href="/actualite-bourse/correction-technique-en-vue-ce7f51dfda8df125">
BANK VONTOBEL/PUT/TOTALENERGIES/72/0.2/19.03.27: Correction technique en vue Le 24 juillet 2026 à 11:39
</a>
<a href="/actualite-bourse/l-europe-reprend-un-peu-de-couleurs-ce7f51dfda8af42c">
CAC 40: L'Europe reprend un peu de couleurs Le 24 juillet 2026 à 11:23
</a>
</body></html>
"""

MARKDOWN_FIXTURE = """
[BANK VONTOBEL/PUT/APPLE/290/0.1/19.03.27: Besoin de souffler Le 21 juillet 2026 à 15:03](https://ch.zonebourse.com/actualite-bourse/besoin-de-souffler-ce7f51d8d88eff21/)
[S&P 500: Hausse initiale de 0.45% Le 21 juillet 2026 à 14:02](/actualite-bourse/hausse-initiale-ce7f51d8d88ef321)
"""

RECOMMENDATIONS_FIXTURE = """
<html><body>
<a href="/actualite-bourse/achat-du-turbo-call-vontobel-mu13v-ce7f50d2dd80f02d">
RUSSELL 2000 TURBO - MU13V - 07/08 Achat du turbo CALL Vontobel MU13V Prix d'exercice Barrière Maturité Elasticité 2 642,36 € Illimité 7.78x CALL
</a>
<a href="/actualite-bourse/nouveau-potentiel-ce7f50d2dd81f426">
GOLD WARRANT - VU91V - 07/08 Nouveau potentiel au-dessus de la résistance Prix d'exercice Maturité Elasticité Delta 4 500,00 € 19/03/2027 6.97x 0.53 CALL
</a>
<a href="/actualite-bourse/actualite-sans-produit-ce7f50d2dd81aaaa">
Actualité sans recommandation Le 7 août 2026 à 10:00
</a>
</body></html>
"""


class FastZonebourseScanV2Tests(unittest.TestCase):
    def test_html_parser_keeps_only_dated_articles(self):
        articles = parse_html_articles(HTML_FIXTURE)
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["kind"], "position_candidate")
        self.assertEqual(articles[0]["publishedAt"], "2026-07-24T11:39+02:00")

    def test_markdown_fallback_and_host_canonicalization(self):
        articles = parse_markdown_articles(MARKDOWN_FIXTURE)
        self.assertEqual(len(articles), 2)
        self.assertEqual(
            articles[0]["url"],
            "https://www.zonebourse.com/actualite-bourse/besoin-de-souffler-ce7f51d8d88eff21",
        )
        self.assertEqual(articles[1]["kind"], "market_analysis")

    def test_rejects_non_article_navigation_links(self):
        self.assertIsNone(canonical_article_url("/actualite-bourse/"))
        self.assertIsNone(canonical_article_url("https://example.com/actualite-bourse/fake-ce123"))

    def test_extracts_all_global_recommendation_cards(self):
        articles = parse_html_articles(RECOMMENDATIONS_FIXTURE, recommendations_only=True)
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["underlying"], "RUSSELL 2000")
        self.assertEqual(articles[0]["productType"], "TURBO")
        self.assertEqual(articles[0]["productCode"], "MU13V")
        self.assertEqual(articles[0]["direction"], "CALL")
        self.assertEqual(articles[1]["title"], "Nouveau potentiel au-dessus de la résistance")

    def test_infers_previous_year_for_december_card_seen_in_january(self):
        parsed = parse_recommendation(
            "CAC 40 WARRANT - AB12C - 20/12 Rebond attendu Prix d'exercice 7000",
            datetime(2027, 1, 5, tzinfo=ZoneInfo("Europe/Paris")),
        )
        self.assertEqual(parsed["publishedAt"], "2026-12-20T00:00+01:00")

    def test_extracts_same_day_card_displayed_with_time(self):
        parsed = parse_recommendation(
            "STMICROELECTRONICS N.V. WARRANT - OJ23V - 11:12 Une zone support en renfort Prix d'exercice 60",
            datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Europe/Paris")),
        )
        self.assertEqual(parsed["productCode"], "OJ23V")
        self.assertEqual(parsed["publishedAt"], "2026-08-10T11:12+02:00")


if __name__ == "__main__":
    unittest.main()
