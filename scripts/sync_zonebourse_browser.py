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
    parse_html_articles,
    parse_markdown_articles,
)
from market_data.euronext_browser import PlaywrightRequester
from sync_zonebourse_positions import (
    CLOSED_URLS,
    OPEN_URLS,
    parse_article_details,
    article_text,
    parse_closed_cards,
    synchronize,
)


def _jina_url(url: str) -> str:
    return f"https://r.jina.ai/http://{url.split('://', 1)[-1]}"


def _article_details_with_fallback(requester, url: str) -> dict:
    details = parse_article_details(requester(url))
    if all(value is not None for value in details.values()):
        return details
    fallback = parse_article_details(requester(_jina_url(url)))
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
) -> dict:
    current_by_url: dict[str, dict] = {}
    source_used = None
    errors: list[str] = []
    for source in OPEN_URLS:
        try:
            html = requester(source)
            cards = parse_html_articles(html, source, recommendations_only=True)
            if cards:
                current_by_url.update((card["url"], card) for card in cards)
                source_used = source
                break
            errors.append(f"{source}: aucune recommandation extraite")
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    if not current_by_url:
        for source_name, source in TEXT_FALLBACK_SOURCES:
            try:
                rendered = requester(source)
                cards = parse_markdown_articles(
                    article_text(rendered),
                    RECOMMENDATIONS_URL,
                    recommendations_only=True,
                )
                if cards:
                    current_by_url.update((card["url"], card) for card in cards)
                    source_used = source_name
                    break
                errors.append(f"{source}: aucune recommandation extraite")
            except Exception as exc:
                errors.append(f"{source}: {exc}")
    if not current_by_url:
        return {"changed": False, "degraded": True, "errors": errors}

    positions_by_code = {
        item.get("mnemo"): item
        for item in document.get("positions", [])
        if item.get("status") == "open"
    }
    fetched = 0
    for card in current_by_url.values():
        existing = positions_by_code.get(card.get("productCode"), {})
        if not any(not existing.get(field) for field in ("isin", "entryPrice", "target", "stop")):
            continue
        if fetched >= max_article_fetches:
            break
        fetched += 1
        try:
            details = _article_details_with_fallback(requester, card["url"])
            card.update({key: value for key, value in details.items() if value is not None})
        except Exception as exc:
            errors.append(f"{card.get('productCode')}: {exc}")

    closed: list[dict] = []
    for source in CLOSED_URLS:
        try:
            closed = parse_closed_cards(requester(source), source, now)
            if closed:
                break
        except Exception as exc:
            errors.append(f"{source}: {exc}")

    result = synchronize(document, list(current_by_url.values()), closed, now)
    quality_changed = refresh_data_quality(document.get("positions", []))
    result.update(
        {
            "changed": bool(result["changed"] or quality_changed),
            "qualityUpdated": quality_changed,
            "articlePagesFetched": fetched,
            "sourceUsed": source_used,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
