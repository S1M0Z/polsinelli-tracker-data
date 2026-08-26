#!/usr/bin/env python3
"""Synchronise Zonebourse through Chromium when direct HTTP sources return 403."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from data_quality import refresh_data_quality
from fast_zonebourse_scan_v2 import (
    PARIS,
    RECOMMENDATIONS_URL,
    TEXT_FALLBACK_SOURCES,
    fetch_text,
    parse_html_articles,
    parse_markdown_articles,
)
from market_data.euronext_browser import PlaywrightRequester
from sync_zonebourse_positions import (
    CLOSED_URLS,
    OPEN_URLS,
    PARTIAL_OPEN_URLS,
    parse_article_details,
    parse_closed_cards,
    synchronize,
)


def _jina_url(url: str) -> str:
    return f"https://r.jina.ai/http://{url.split('://', 1)[-1]}"


def _article_details_with_fallback(requester, text_requester, url: str) -> dict:
    details = parse_article_details(requester(url))
    if all(value is not None for value in details.values()):
        return details
    fallback = parse_article_details(text_requester(_jina_url(url)))
    return {
        field: value if value is not None else fallback.get(field)
        for field, value in details.items()
    }


def write_json(path: Path, document: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def synchronize_with_browser(
    document: dict,
    requester,
    now: datetime,
    *,
    max_article_fetches: int = 12,
    text_requester=fetch_text,
) -> dict:
    current_by_url: dict[str, dict] = {}
    sources_used: list[str] = []
    errors: list[str] = []
    for source in OPEN_URLS:
        try:
            source_cards: dict[str, dict] = {}
            for page in range(1, 11):
                url = source if page == 1 else f"{source}?p={page}"
                cards = parse_html_articles(requester(url), url, recommendations_only=True)
                new_cards = [card for card in cards if card["url"] not in source_cards]
                if not new_cards:
                    break
                source_cards.update((card["url"], card) for card in new_cards)
            if source_cards:
                current_by_url.update(source_cards)
                sources_used.append(source)
            else:
                errors.append(f"{source}: aucune recommandation extraite")
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    if not current_by_url:
        for source_name, source in TEXT_FALLBACK_SOURCES:
            try:
                cards = parse_markdown_articles(
                    text_requester(source),
                    RECOMMENDATIONS_URL,
                    recommendations_only=True,
                )
                if cards:
                    current_by_url.update((card["url"], card) for card in cards)
                    sources_used.append(source_name)
                else:
                    errors.append(f"{source}: aucune recommandation extraite")
            except Exception as exc:
                errors.append(f"{source}: {exc}")
    partial_source = False
    if not current_by_url:
        for source in PARTIAL_OPEN_URLS:
            try:
                cards = parse_html_articles(
                    requester(source), source, recommendations_only=True
                )
                if cards:
                    current_by_url.update((card["url"], card) for card in cards)
                    sources_used.append(source)
                    partial_source = True
                    break
                errors.append(f"{source}: aucune recommandation extraite")
            except Exception as exc:
                errors.append(f"{source}: {exc}")
    if not current_by_url:
        meta = document.setdefault("meta", {})
        health = meta.setdefault("sourceHealth", {})
        failures = int(health.get("consecutiveFailures", 0)) + 1
        health.update({
            "status": "source_unavailable",
            "attemptedAt": now.isoformat(timespec="seconds"),
            "consecutiveFailures": failures,
            "errors": errors[-10:],
        })
        return {"changed": True, "degraded": True, "errors": errors, "consecutiveFailures": failures}

    if partial_source:
        # Preserve recommendations not shown in the short synthesis page. The
        # synchronizer may still add new cards, while explicit history cards
        # remain the only authority allowed to close a position.
        for position in document.get("positions", []):
            if position.get("status") != "open" or not position.get("url"):
                continue
            current_by_url.setdefault(position["url"], {
                "url": position["url"],
                "title": position.get("note") or position.get("asset") or "Recommendation",
                "publishedAt": position.get("publishedAt") or position.get("entryDate"),
                "publishedAtPrecision": position.get("publishedAtPrecision", "date"),
                "kind": "position_candidate",
                "underlying": position.get("asset"),
                "productType": str(position.get("productType") or "Turbo").upper(),
                "productCode": position.get("mnemo"),
                "direction": position.get("direction"),
            })

    positions_by_code = {
        item.get("mnemo"): item
        for item in document.get("positions", [])
        if item.get("status") == "open"
    }
    fetched = 0
    queue = document.setdefault("meta", {}).setdefault("articleEnrichmentQueue", [])
    cards_by_url = dict(current_by_url)
    ordered_urls = list(dict.fromkeys([*queue, *cards_by_url]))
    remaining_queue: list[str] = []
    for url in ordered_urls:
        card = cards_by_url.get(url)
        if card is None:
            continue
        existing = positions_by_code.get(card.get("productCode"), {})
        if not any(not existing.get(field) for field in ("isin", "entryPrice", "target", "stop")):
            continue
        if fetched >= max_article_fetches:
            remaining_queue.append(url)
            continue
        fetched += 1
        try:
            details = _article_details_with_fallback(
                requester, text_requester, card["url"]
            )
            card.update({key: value for key, value in details.items() if value is not None})
        except Exception as exc:
            errors.append(f"{card.get('productCode')}: {exc}")
            remaining_queue.append(url)
    document["meta"]["articleEnrichmentQueue"] = remaining_queue

    closed: list[dict] = []
    closed_by_url: dict[str, dict] = {}
    for source in CLOSED_URLS:
        try:
            for page in range(1, 6):
                url = source if page == 1 else f"{source}?p={page}"
                rows = parse_closed_cards(requester(url), url, now)
                if not rows and page > 1:
                    break
                closed_by_url.update((row["url"], row) for row in rows)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    closed = list(closed_by_url.values())

    result = synchronize(document, list(current_by_url.values()), closed, now)
    previous_health = document.setdefault("meta", {}).get("sourceHealth")
    source_health = {
        "status": "ok" if not errors else "degraded",
        "attemptedAt": now.isoformat(timespec="seconds"),
        "lastSuccessAt": now.isoformat(timespec="seconds"),
        "consecutiveFailures": 0,
        "errors": errors[-10:],
    }
    document["meta"]["sourceHealth"] = source_health
    health_changed = previous_health != source_health
    quality_changed = refresh_data_quality(document.get("positions", []))
    result.update(
        {
            "changed": bool(result["changed"] or quality_changed or health_changed),
            "qualityUpdated": quality_changed,
            "articlePagesFetched": fetched,
            "sourceUsed": ",".join(sources_used) if sources_used else None,
            "sourcesUsed": sources_used,
            "enrichmentQueueSize": len(remaining_queue),
            "errors": errors,
            "degraded": False,
        }
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--max-article-fetches", type=int, default=12)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = args.root.resolve() / "positions.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    requester = PlaywrightRequester(
        timeout_ms=35_000,
        settle_ms=1_500,
        allow_http_errors=True,
        locale="fr-FR",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        reduce_automation_signals=True,
    )
    try:
        result = synchronize_with_browser(
            document,
            requester.get,
            datetime.now(PARIS),
            max_article_fetches=max(1, args.max_article_fetches),
        )
    finally:
        requester.close()
    if result.get("changed"):
        write_json(path, document)
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result.get("consecutiveFailures", 0) >= 3 else 0


if __name__ == "__main__":
    raise SystemExit(main())
