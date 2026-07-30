#!/usr/bin/env python3
"""Détection rapide stricte des publications affichées sur la page auteur."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

AUTHOR_URL = "https://www.zonebourse.com/auteur/laurent-polsinelli"
PARIS = ZoneInfo("Europe/Paris")
DATE_RE = re.compile(r"\bLe\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\s+à\s+(\d{2}):(\d{2})\b", re.I)
ARTICLE_RE = re.compile(r"^/actualite-bourse/[^/]+-ce[0-9a-f]+/?$", re.I)
MONTHS = {"janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,"juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12}
POSITION_WORDS = ("TURBO", "WARRANT", "MINI FUTURE", "MINI-FUTURE", "SPRINTER", "OPTIONSSCHEIN", "/CALL/", "/PUT/")

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.href = None
        self.parts = []
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href and "/actualite-bourse/" in href:
                self.href, self.parts = href, []
    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href is not None:
            text = " ".join(" ".join(self.parts).split())
            if text:
                self.links.append((self.href, text))
            self.href, self.parts = None, []

def fetch_html():
    req = Request(AUTHOR_URL, headers={"User-Agent":"Mozilla/5.0 (compatible; PolsinelliTracker/2.0)","Accept-Language":"fr-FR,fr;q=0.9"})
    with urlopen(req, timeout=25) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")

def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None, None
    day, month_name, year, hour, minute = match.groups()
    month = MONTHS.get(month_name.lower())
    if not month:
        return None, None
    published = datetime(int(year), month, int(day), int(hour), int(minute), tzinfo=PARIS)
    return text[:match.start()].strip(" :-"), published.isoformat(timespec="minutes")

def classify(title):
    upper = title.upper()
    if upper.startswith("CAC 40") or upper.startswith("S&P 500"):
        return "market_analysis"
    if any(word in upper for word in POSITION_WORDS):
        return "position_candidate"
    return "other"

def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def outputs(changed, new):
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    candidates = [item for item in new if item["kind"] == "position_candidate"]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"changed={'true' if changed else 'false'}\n")
        handle.write(f"position_count={len(candidates)}\n")
        handle.write("position_urls=" + json.dumps([x["url"] for x in candidates], ensure_ascii=False) + "\n")
        handle.write("position_titles=" + json.dumps([x["title"] for x in candidates], ensure_ascii=False) + "\n")
        handle.write("position_published=" + json.dumps([x["publishedAt"] for x in candidates], ensure_ascii=False) + "\n")

def main():
    parser = Parser(); parser.feed(fetch_html())
    articles, seen = [], set()
    for href, raw in parser.links:
        url = urljoin(AUTHOR_URL, href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.netloc not in {"www.zonebourse.com", "zonebourse.com"} or not ARTICLE_RE.match(parsed.path) or url in seen:
            continue
        title, published = parse_date(raw)
        # Point essentiel : les liens génériques injectés dans la page n'ont pas la date auteur.
        if not title or not published:
            continue
        seen.add(url)
        articles.append({"url":url,"title":title,"publishedAt":published,"kind":classify(title)})
    if not articles:
        raise RuntimeError("Aucun article auteur daté extrait; état non modifié")

    state_path, article_path = Path("fast-scan.json"), Path("article-state.json")
    state = load(state_path, {"meta":{},"latest":None,"alerts":[]})
    article_state = load(article_path, {"articles":{}})
    known = {x.get("url") for x in state.get("alerts", []) if isinstance(x, dict)}
    known |= {x.get("url") for x in article_state.get("articles", {}).values() if isinstance(x, dict)}
    detected = datetime.now(PARIS).isoformat(timespec="seconds")
    new = []
    for article in articles:
        if article["url"] not in known:
            new.append({**article,"detectedAt":detected,"status":"detected"})
    if new:
        state.setdefault("meta", {}).update({"updatedAt":detected,"source":AUTHOR_URL,"scanIntervalMinutes":5,"description":"Détection stricte des liens datés présents dans la liste auteur."})
        state["latest"] = {**articles[0],"seenAt":detected}
        state["alerts"] = (new + state.get("alerts", []))[:200]
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outputs(bool(new), new)
    print(json.dumps({"changed":bool(new),"newArticles":len(new),"positionCandidates":sum(x["kind"]=="position_candidate" for x in new)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
