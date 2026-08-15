#!/usr/bin/env python3
"""Synchronise les positions ouvertes et les sorties explicites de Zonebourse."""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .fast_zonebourse_scan_v2 import (
        PARIS, Parser, canonical_article_url, fetch_text, parse_html_articles,
    )
except ImportError:  # Exécution directe: python scripts/sync_zonebourse_positions.py
    from fast_zonebourse_scan_v2 import (
        PARIS, Parser, canonical_article_url, fetch_text, parse_html_articles,
    )

OPEN_URLS = (
    "https://www.zonebourse.com/bourse/derives/recommandations/",
    "https://ch.zonebourse.com/bourse/derives/recommandations/",
)
CLOSED_URLS = (
    "https://www.zonebourse.com/bourse/derives/recommandations/cloturees/",
    "https://ch.zonebourse.com/bourse/derives/recommandations/cloturees/",
)
CLOSED_CARD_RE = re.compile(
    r"^(?P<asset>.+?)\s+(?P<product>TURBO|WARRANT)\s+-\s+"
    r"(?P<code>[A-Z0-9]+)\s+-\s+(?P<day>\d{2})/(?P<month>\d{2})\s+"
    r"(?P<title>.+?)\s+Prix d.exercice\s*Entrée\s*Sortie\s*Performance\s+"
    r"(?:[-\d\s,.]+\s*€|-)\s+(?P<entry>\d+[,.]\d+)\s+"
    r"(?P<exit>\d+[,.]\d+)\s+(?P<performance>[+-]\d+[,.]\d+)\s*%\s+SORTIE$",
    re.IGNORECASE,
)


def number(value: str) -> float:
    return float(value.replace(" ", "").replace("\u202f", "").replace(",", "."))


def slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def dated(day: int, month: int, now: datetime) -> datetime:
    value = datetime(now.year, month, day, tzinfo=PARIS)
    return value.replace(year=now.year - 1) if value > now + timedelta(days=31) else value


def fetch_first(urls: tuple[str, ...]) -> tuple[str, str]:
    errors: list[str] = []
    for url in urls:
        try:
            return fetch_text(url), url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(" | ".join(errors))


def fetch_pages(base_urls: tuple[str, ...], max_pages: int = 10) -> list[tuple[str, str]]:
    """Charge toutes les pages disponibles depuis la première édition joignable."""
    errors: list[str] = []
    for base_url in base_urls:
        pages: list[tuple[str, str]] = []
        try:
            for page in range(1, max_pages + 1):
                url = base_url if page == 1 else f"{base_url}?p={page}"
                html = fetch_text(url)
                parser = Parser()
                parser.feed(html)
                if page > 1 and not parser.links:
                    break
                pages.append((html, url))
            if pages:
                return pages
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    raise RuntimeError(" | ".join(errors))


def parse_closed_cards(html: str, base_url: str, now: datetime) -> list[dict]:
    parser = Parser()
    parser.feed(html)
    rows: list[dict] = []
    seen: set[str] = set()
    for href, raw in parser.links:
        text = " ".join(raw.split())
        match = CLOSED_CARD_RE.match(text)
        url = canonical_article_url(href, base_url)
        if not match or not url or url in seen:
            continue
        seen.add(url)
        closed_at = dated(int(match.group("day")), int(match.group("month")), now)
        rows.append(
            {
                "asset": match.group("asset").strip(),
                "productType": match.group("product").title(),
                "mnemo": match.group("code").upper(),
                "title": match.group("title").strip(),
                "entryPrice": number(match.group("entry")),
                "exitPrice": number(match.group("exit")),
                "realizedReturnPct": number(match.group("performance")),
                "exitDate": closed_at.date().isoformat(),
                "url": url,
            }
        )
    return rows


def new_position(card: dict, detected_at: str) -> dict:
    entry_date = card["publishedAt"][:10]
    return {
        "id": f"{slug(card['underlying'])}-{card['productCode'].lower()}-{entry_date.replace('-', '')}",
        "asset": card["underlying"],
        "productType": card["productType"].title(),
        "direction": card.get("direction")
        or ("PUT" if "PUT" in card["title"].upper() else "CALL"),
        "mnemo": card["productCode"],
        "isin": None,
        "status": "open",
        "entryPrice": None,
        "currentPrice": None,
        "exitPrice": None,
        "entryDate": entry_date,
        "exitDate": None,
        "currency": "EUR",
        "url": card["url"],
        "publishedAt": card["publishedAt"],
        "publishedAtPrecision": card.get("publishedAtPrecision", "minute"),
        "detectedAt": detected_at,
        "note": f"{card['title']}. Prix d'entrée et ISIN à confirmer depuis la fiche article.",
        "sourceConfidence": "medium",
    }


def synchronize(document: dict, current: list[dict], closed: list[dict], now: datetime) -> dict:
    positions = document.setdefault("positions", [])
    open_by_code = {
        item.get("mnemo"): item for item in positions if item.get("status") == "open"
    }
    detected_at = now.isoformat(timespec="seconds")
    added = 0
    closed_count = 0
    changed = False

    for card in current:
        existing = open_by_code.get(card["productCode"])
        if existing:
            if existing.get("url") != card["url"]:
                existing["url"] = card["url"]
                changed = True
            if not existing.get("publishedAt"):
                existing["publishedAt"] = card["publishedAt"]
                changed = True
            precision = card.get("publishedAtPrecision", "minute")
            if existing.get("publishedAtPrecision") != precision:
                existing["publishedAtPrecision"] = precision
                changed = True
            if not existing.get("detectedAt"):
                existing["detectedAt"] = detected_at
                changed = True
            for destination, source in (
                ("asset", "underlying"),
                ("productType", "productType"),
                ("direction", "direction"),
            ):
                value = card.get(source)
                if value and not existing.get(destination):
                    existing[destination] = value.title() if destination == "productType" else value
                    changed = True
            continue
        position = new_position(card, detected_at)
        positions.append(position)
        open_by_code[position["mnemo"]] = position
        added += 1
        changed = True

    for row in closed:
        position = open_by_code.get(row["mnemo"])
        if not position:
            continue
        position.update(
            {
                "status": "closed",
                "entryPrice": row["entryPrice"],
                "currentPrice": None,
                "exitPrice": row["exitPrice"],
                "exitDate": row["exitDate"],
                "quoteAt": None,
                "url": row["url"],
                "note": f"{row['title']} ({row['realizedReturnPct']:+.2f} % selon Zonebourse).",
                "realizedReturnPct": row["realizedReturnPct"],
                "sourceConfidence": "high",
            }
        )
        for key in (
            "bid", "ask", "marketDataProvider", "marketDataSourceUrl",
            "marketDataRetrievedAt", "marketDataDelayMinutes",
        ):
            position.pop(key, None)
        closed_count += 1
        changed = True

    meta = document.setdefault("meta", {})
    expected_meta = {
            "generatedAt": detected_at,
            "source": "Zonebourse / recommandations produits dérivés",
            "lastPublicationCheckedAt": detected_at,
            "openRecommendationCount": len(current),
            "automaticSync": True,
    }
    if meta.get("automaticSync") is not True or meta.get("openRecommendationCount") != len(current):
        changed = True
    if changed:
        meta.update(expected_meta)
    return {
        "added": added,
        "closed": closed_count,
        "openSeen": len(current),
        "changed": changed,
    }


def main() -> int:
    now = datetime.now(PARIS)
    positions_path = Path("positions.json")
    try:
        current_by_url: dict[str, dict] = {}
        for open_html, open_source in fetch_pages(OPEN_URLS):
            for card in parse_html_articles(open_html, open_source, recommendations_only=True):
                current_by_url[card["url"]] = card
        current = list(current_by_url.values())
        if not current:
            raise RuntimeError("aucune recommandation ouverte extraite")
        closed_by_url: dict[str, dict] = {}
        for closed_html, closed_source in fetch_pages(CLOSED_URLS, max_pages=5):
            for card in parse_closed_cards(closed_html, closed_source, now):
                closed_by_url[card["url"]] = card
        closed = list(closed_by_url.values())
    except Exception as exc:
        print(f"::warning title=Synchronisation Zonebourse ignorée::{exc}")
        return 0

    document = json.loads(positions_path.read_text(encoding="utf-8"))
    result = synchronize(document, current, closed, now)
    if result["changed"]:
        positions_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(
                f"changed={'true' if result['changed'] else 'false'}\n"
                f"added={result['added']}\nclosed={result['closed']}\n"
            )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"sync_zonebourse_positions error: {exc}", file=sys.stderr)
        raise
