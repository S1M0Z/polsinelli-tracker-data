#!/usr/bin/env python3
"""Détection rapide des recommandations de produits dérivés Zonebourse.

Le scanner essaie d'abord la page globale officielle, puis un rendu texte de
secours. Une indisponibilité temporaire de la source ne modifie jamais l'état et
ne fait pas échouer le workflow toutes les cinq minutes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

RECOMMENDATIONS_URL = "https://www.zonebourse.com/bourse/derives/recommandations/"
DIRECT_SOURCES = (
    ("zonebourse-fr", RECOMMENDATIONS_URL),
    ("zonebourse-ch", "https://ch.zonebourse.com/bourse/derives/recommandations/"),
)
TEXT_FALLBACK_SOURCES = (
    (
        "jina-zonebourse-fr",
        "https://r.jina.ai/http://www.zonebourse.com/bourse/derives/recommandations/",
    ),
)
PARIS = ZoneInfo("Europe/Paris")
DATE_RE = re.compile(
    r"\bLe\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\s+à\s+(\d{2}):(\d{2})\b",
    re.IGNORECASE,
)
ARTICLE_RE = re.compile(r"^/actualite-bourse/[^/]+-ce[0-9a-f]+/?$", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{5,700})\]\(([^)\s]+)\)")
RECOMMENDATION_RE = re.compile(
    r"^(?P<underlying>.+?)\s+(?P<product>TURBO|WARRANT)\s+-\s+"
    r"(?P<code>[A-Z0-9]+)\s+-\s+"
    r"(?:(?P<day>\d{2})/(?P<month>\d{2})|(?P<hour>\d{2}):(?P<minute>\d{2}))\s+"
    r"(?P<title>.+?)(?=\s+Prix d.exercice|\s+Barrière|\s+Maturité|$)",
    re.IGNORECASE,
)
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
POSITION_WORDS = (
    "TURBO",
    "WARRANT",
    "MINI FUTURE",
    "MINI-FUTURE",
    "SPRINTER",
    "OPTIONSSCHEIN",
    "/CALL/",
    "/PUT/",
)


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href and "/actualite-bourse/" in href:
                self.href, self.parts = href, []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.href is not None:
            text = " ".join(" ".join(self.parts).split())
            if text:
                self.links.append((self.href, text))
            self.href, self.parts = None, []


def parse_date(text: str) -> tuple[str | None, str | None]:
    match = DATE_RE.search(text)
    if not match:
        return None, None
    day, month_name, year, hour, minute = match.groups()
    month = MONTHS.get(month_name.lower())
    if not month:
        return None, None
    title = text[: match.start()].strip(" :-")
    if not title:
        return None, None
    published = datetime(
        int(year), month, int(day), int(hour), int(minute), tzinfo=PARIS
    )
    return title, published.isoformat(timespec="minutes")


def classify(title: str) -> str:
    upper = title.upper()
    if upper.startswith("CAC 40") or upper.startswith("S&P 500"):
        return "market_analysis"
    if any(word in upper for word in POSITION_WORDS):
        return "position_candidate"
    return "other"


def canonical_article_url(
    href: str, base_url: str = RECOMMENDATIONS_URL
) -> str | None:
    url = urljoin(base_url, href).split("#", 1)[0]
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {
        "www.zonebourse.com",
        "zonebourse.com",
        "ch.zonebourse.com",
    }:
        return None
    path = parsed.path.rstrip("/")
    if not ARTICLE_RE.match(path):
        return None
    return f"https://www.zonebourse.com{path}"


def parse_recommendation(text: str, now: datetime | None = None) -> dict | None:
    """Extrait la carte compacte affichée dans les recommandations en cours."""
    compact = " ".join(text.split())
    match = RECOMMENDATION_RE.match(compact)
    if not match:
        return None
    reference = now or datetime.now(PARIS)
    if match.group("day"):
        published = datetime(
            reference.year,
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=PARIS,
        )
    else:
        published = reference.replace(
            hour=int(match.group("hour")),
            minute=int(match.group("minute")),
            second=0,
            microsecond=0,
        )
    # La page omet l'année. Au passage de décembre à janvier, une date qui
    # semblerait très future appartient nécessairement à l'année précédente.
    if match.group("day") and published > reference + timedelta(days=31):
        published = published.replace(year=reference.year - 1)
    product_type = match.group("product").upper()
    direction_match = re.search(r"\b(CALL|PUT)\s*$", compact, re.IGNORECASE)
    return {
        "title": match.group("title").strip(),
        "publishedAt": published.isoformat(timespec="minutes"),
        "publishedAtPrecision": "date" if match.group("day") else "minute",
        "kind": "position_candidate",
        "underlying": match.group("underlying").strip(),
        "productType": product_type,
        "productCode": match.group("code").upper(),
        "direction": direction_match.group(1).upper() if direction_match else None,
    }


def _articles_from_links(
    links: list[tuple[str, str]],
    base_url: str,
    recommendations_only: bool = False,
) -> list[dict]:
    articles: list[dict] = []
    seen: set[str] = set()
    for href, raw in links:
        url = canonical_article_url(href, base_url)
        if not url or url in seen:
            continue
        compact = " ".join(raw.split())
        recommendation = parse_recommendation(compact)
        if recommendation:
            article = {"url": url, **recommendation}
        else:
            if recommendations_only:
                continue
            title, published = parse_date(compact)
            if not title or not published:
                continue
            article = {
                "url": url,
                "title": title,
                "publishedAt": published,
                "kind": classify(title),
            }
        seen.add(url)
        articles.append(article)
    return articles


def parse_html_articles(
    html: str,
    base_url: str = RECOMMENDATIONS_URL,
    recommendations_only: bool = False,
) -> list[dict]:
    parser = Parser()
    parser.feed(html)
    return _articles_from_links(parser.links, base_url, recommendations_only)


def parse_markdown_articles(
    markdown: str,
    base_url: str = RECOMMENDATIONS_URL,
    recommendations_only: bool = False,
) -> list[dict]:
    links = [(href, text) for text, href in MARKDOWN_LINK_RE.findall(markdown)]
    return _articles_from_links(links, base_url, recommendations_only)


def fetch_text(url: str, attempts: int = 2) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urlopen(request, timeout=25) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    raise RuntimeError(f"{type(last_error).__name__}: {last_error}")


def scan_sources() -> tuple[list[dict], str | None, list[str]]:
    errors: list[str] = []
    for source_name, source_url in DIRECT_SOURCES:
        try:
            articles = parse_html_articles(
                fetch_text(source_url), source_url, recommendations_only=True
            )
            if articles:
                return articles, source_name, errors
            errors.append(f"{source_name}: aucune recommandation extraite")
        except Exception as exc:
            errors.append(f"{source_name}: {exc}")

    for source_name, source_url in TEXT_FALLBACK_SOURCES:
        try:
            articles = parse_markdown_articles(
                fetch_text(source_url),
                RECOMMENDATIONS_URL,
                recommendations_only=True,
            )
            if articles:
                return articles, source_name, errors
            errors.append(f"{source_name}: aucune recommandation extraite")
        except Exception as exc:
            errors.append(f"{source_name}: {exc}")
    return [], None, errors


def load(path: Path, default: dict) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_outputs(
    changed: bool,
    new: list[dict],
    source_status: str,
    source_used: str | None,
    source_error: str = "",
) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    candidates = [item for item in new if item["kind"] == "position_candidate"]
    safe_error = " ".join(source_error.split())[:1500]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"changed={'true' if changed else 'false'}\n")
        handle.write(f"position_count={len(candidates)}\n")
        handle.write(f"source_status={source_status}\n")
        handle.write(f"source_used={source_used or ''}\n")
        handle.write(f"source_error={safe_error}\n")
        handle.write(
            "position_urls="
            + json.dumps([x["url"] for x in candidates], ensure_ascii=False)
            + "\n"
        )
        handle.write(
            "position_titles="
            + json.dumps([x["title"] for x in candidates], ensure_ascii=False)
            + "\n"
        )
        handle.write(
            "position_published="
            + json.dumps([x["publishedAt"] for x in candidates], ensure_ascii=False)
            + "\n"
        )


def main() -> int:
    articles, source_used, errors = scan_sources()
    if not articles:
        message = " | ".join(errors) or "aucune source exploitable"
        write_outputs(False, [], "degraded", None, message)
        print(f"::warning title=Scanner Zonebourse dégradé::{message}")
        print(json.dumps({"changed": False, "sourceStatus": "degraded", "errors": errors}, ensure_ascii=False))
        return 0

    state_path, article_path = Path("fast-scan.json"), Path("article-state.json")
    state = load(state_path, {"meta": {}, "latest": None, "alerts": []})
    article_state = load(article_path, {"articles": {}})
    known = {
        item.get("url")
        for item in state.get("alerts", [])
        if isinstance(item, dict) and item.get("url")
    }
    known |= {
        item.get("url")
        for item in article_state.get("articles", {}).values()
        if isinstance(item, dict) and item.get("url")
    }

    detected = datetime.now(PARIS).isoformat(timespec="seconds")
    new = [
        {**article, "detectedAt": detected, "status": "detected"}
        for article in articles
        if article["url"] not in known
    ]
    if new:
        state.setdefault("meta", {}).update(
            {
                "updatedAt": detected,
                "source": RECOMMENDATIONS_URL,
                "sourceUsed": source_used,
                "scanIntervalMinutes": 5,
                "description": "Détection multi-source de toutes les recommandations Zonebourse de Turbos et Warrants en cours.",
            }
        )
        state["latest"] = {**articles[0], "seenAt": detected}
        state["alerts"] = (new + state.get("alerts", []))[:200]
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    write_outputs(bool(new), new, "ok", source_used, " | ".join(errors))
    print(
        json.dumps(
            {
                "changed": bool(new),
                "newArticles": len(new),
                "positionCandidates": sum(x["kind"] == "position_candidate" for x in new),
                "sourceStatus": "ok",
                "sourceUsed": source_used,
                "fallbackErrors": errors,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"fast_zonebourse_scan_v2 error: {exc}", file=sys.stderr)
        raise
