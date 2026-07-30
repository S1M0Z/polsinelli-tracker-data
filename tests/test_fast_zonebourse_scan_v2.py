import unittest

from scripts.fast_zonebourse_scan_v2 import (
    canonical_article_url,
    parse_html_articles,
    parse_markdown_articles,
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


if __name__ == "__main__":
    unittest.main()
