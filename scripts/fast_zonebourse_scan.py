#!/usr/bin/env python3
"""Détecte rapidement les nouvelles publications de Laurent Polsinelli.

Ce scanner ne transforme jamais seul un article en ordre d'achat. Il repère les
nouveaux liens, les classe et alimente fast-scan.json. La synchronisation
complète confirme ensuite les données du produit et recalcule l'éligibilité.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

AUTHOR_URL = "https://www.zonebourse.com/auteur/laurent-polsinelli"
PARIS = ZoneInfo("Europe/Paris")
MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}
DATE_RE = re.compile(
    r"\bLe\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\s+à\s+(\d{2}):(\d{2})\b",
    re.IGNORECASE,
)
POSITION_KEYWORDS = (
    "TURBO",
    "WARRANT",
    "MINI FUTURE",
    "MINI-FUTURE",
    "SPRINTER",
    "OPTIONSSCHEIN",
    "CALL/",
    "/CALL/",
    "PUT/",
    "/PUT/",
)


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    publishedAt: str | None
    kind: str


class ArticleLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href and "/actualite-bourse/" in href:
            self._href = href
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = " ".join(" ".join(self._parts).split())
            if text:
                self.links.append((self._href, text))
            self._href = None
            self._parts = []


def now_iso() -> str:
    return datetime.now(PARIS).isoformat(timespec="seconds")


def fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; PolsinelliTracker/1.0; "
                "+https://github.com/S1M0Z/polsinelli-tracker-data)"
            ),
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
        },
    )
    with urlopen(request, timeout=25) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_title_and_date(text: str) -> tuple[str, str | None]:
    match = DATE_RE.search(text)
    if not match:
        return text.strip(), None

    day, month_name, year, hour, minute = match.groups()
    title = text[: match.start()].strip(" :-")
    month = MONTHS.get(month_name.lower())
    if month is None:
        return title, None

    published = datetime(
        int(year), month, int(day), int(hour), int(minute), tzinfo=PARIS
    )
    return title, published.isoformat(timespec="minutes")


def classify(title: str) -> str:
    upper = title.upper()
    if upper.startswith("CAC 40") or upper.startswith("S&P 500"):
        return "market_analysis"
    if any(keyword in upper for keyword in POSITION_KEYWORDS):
        return "position_candidate"
    return "other"


def parse_articles(html: str) -> list[Article]:
    parser = ArticleLinkParser()
    parser.feed(html)

    seen: set[str] = set()
    articles: list[Article] = []
    for href, raw_text in parser.links:
        url = urljoin(AUTHOR_URL, href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.netloc not in {"www.zonebourse.com", "zonebourse.com"}:
            continue
        if url in seen:
            continue

        title, published_at = parse_title_and_date(raw_text)
        if not title:
            continue

        seen.add(url)
        articles.append(
            Article(
                url=url,
                title=title,
                publishedAt=published_at,
                kind=classify(title),
            )
        )
    return articles


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def known_urls(article_state_path: Path, fast_state: dict) -> set[str]:
    known = {
        item.get("url")
        for item in fast_state.get("alerts", [])
        if isinstance(item, dict) and item.get("url")
    }

    article_state = load_json(article_state_path, {"articles": {}})
    for item in article_state.get("articles", {}).values():
        if isinstance(item, dict) and item.get("url"):
            known.add(item["url"])
    return known


def write_outputs(changed: bool, new_articles: list[dict]) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return

    candidates = [
        article for article in new_articles if article.get("kind") == "position_candidate"
    ]
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"changed={'true' if changed else 'false'}\n")
        output.write(f"position_count={len(candidates)}\n")
        output.write(
            "position_urls="
            + json.dumps([article["url"] for article in candidates], ensure_ascii=False)
            + "\n"
        )
        output.write(
            "position_titles="
            + json.dumps([article["title"] for article in candidates], ensure_ascii=False)
            + "\n"
        )
        output.write(
            "position_published="
            + json.dumps(
                [article.get("publishedAt") for article in candidates],
                ensure_ascii=False,
            )
            + "\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html-file", type=Path)
    parser.add_argument("--state", type=Path, default=Path("fast-scan.json"))
    parser.add_argument(
        "--article-state", type=Path, default=Path("article-state.json")
    )
    args = parser.parse_args()

    html = (
        args.html_file.read_text(encoding="utf-8")
        if args.html_file
        else fetch_html(AUTHOR_URL)
    )
    articles = parse_articles(html)
    if not articles:
        raise RuntimeError(
            "Aucun article Zonebourse extrait; arrêt sans modifier l'état."
        )

    state = load_json(args.state, {"meta": {}, "latest": None, "alerts": []})
    state.setdefault("meta", {})
    state.setdefault("alerts", [])

    known = known_urls(args.article_state, state)
    detected_at = now_iso()
    new_articles: list[dict] = []
    for article in articles:
        if article.url in known:
            continue
        item = asdict(article)
        item.update({"detectedAt": detected_at, "status": "detected"})
        new_articles.append(item)

    changed = bool(new_articles)
    if changed:
        newest = articles[0]
        state["meta"].update(
            {
                "updatedAt": detected_at,
                "source": AUTHOR_URL,
                "scanIntervalMinutes": 5,
                "description": (
                    "Détection rapide. Une position reste PENDING_DATA jusqu'à "
                    "confirmation du produit, de la cotation et du risque."
                ),
            }
        )
        state["latest"] = {**asdict(newest), "seenAt": detected_at}
        state["alerts"] = (new_articles + state["alerts"])[:200]
        args.state.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    write_outputs(changed, new_articles)
    print(
        json.dumps(
            {
                "changed": changed,
                "newArticles": len(new_articles),
                "positionCandidates": sum(
                    article["kind"] == "position_candidate"
                    for article in new_articles
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - visible in Actions logs
        print(f"fast_zonebourse_scan error: {exc}", file=sys.stderr)
        raise
