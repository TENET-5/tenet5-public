# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T03:25:00Z
"""liril_hears — LIRIL's ears. CPU-only external-source poller.

Periodically checks a small set of primary-source feeds relevant to the
Canadian accountability investigation, publishes new items on NATS so
the thinking loop can decide whether any warrants a backlog task.

Published subjects (NATS on 4223):
  tenet5.liril.hear.news     — CBC/Globe/Star/CP political feed hits
  tenet5.liril.hear.parl     — House Publications daily order paper
  tenet5.liril.hear.court    — Federal Court / Supreme Court docket changes
  tenet5.liril.hear.officer  — Ethics Commissioner / AG / PBO latest

First iteration: polls FEEDS list every FEED_POLL_SECONDS. Dedup by
URL hash. Writes the last seen set to data/.liril_hears_seen.json so
restarts don't re-fire on stale items.

Dependencies: requests (stdlib-equivalent via urllib), feedparser
(optional; falls back to regex RSS parser if not installed).

Resource use: ~15 MB RAM, negligible CPU between polls, ~1 MB network
per poll cycle. Safe during gaming.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
# 2026-04-19: site-wide subprocess no-window shim
try:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    try: import _liril_subprocess_nowindow  # noqa: F401
    except Exception: pass
except Exception: pass
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

os.environ.setdefault("NATS_URL", "nats://127.0.0.1:4223")
import nats  # type: ignore

SITE = Path(r"E:\TENET-5.github.io")
SEEN_FILE = SITE / "data" / ".liril_hears_seen.json"
QUEUE = SITE / "data" / "liril_perception_queue.jsonl"

FEED_POLL_SECONDS = 1800   # 30 min between polls — polite to servers
HTTP_TIMEOUT = 15

# Conservative feed list. Each entry: (subject_leaf, feed_url, notes).
# Curated for Canadian-federal-accountability relevance. Add more in
# follow-up — start with 3-5 to avoid rate-limit issues.
FEEDS = [
    ("news",    "https://www.cbc.ca/cmlink/rss-politics",             "CBC Politics"),
    ("news",    "https://rss.cbc.ca/lineup/topstories.xml",           "CBC top stories"),
    ("parl",    "https://www.ourcommons.ca/DocumentViewer/en/house/latest-sitting/order-notice/order-notice.xml", "House of Commons order paper"),
    # PBO publication RSS exists but tends to change — leave as follow-up.
    # CanLII has no public RSS; would need HTML scrape + diff.
]


def _log(tag: str, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"  [{stamp}] [HEARS/{tag}] {msg}", flush=True)


def item_hash(link: str, title: str) -> str:
    return hashlib.sha256((link + "|" + title).encode("utf-8")).hexdigest()[:16]


def parse_rss(xml: str) -> list[dict]:
    """Minimal RSS/Atom parser — extracts (title, link, pub_date, summary).
    Uses feedparser if installed, else regex.
    """
    try:
        import feedparser  # type: ignore
        d = feedparser.parse(xml)
        out = []
        for e in d.entries[:40]:
            out.append({
                "title":    (e.get("title") or "").strip(),
                "link":     (e.get("link")  or "").strip(),
                "pub_date": (e.get("published") or e.get("updated") or "").strip(),
                "summary":  (e.get("summary") or "").strip()[:500],
            })
        return out
    except ImportError:
        pass
    # Regex fallback — handles RSS 2.0 and simple Atom
    items = []
    for m in re.finditer(r"<(?:item|entry)[\s\S]*?</(?:item|entry)>", xml):
        block = m.group(0)
        title = ""
        link = ""
        pub_date = ""
        summary = ""
        tm = re.search(r"<title[^>]*>([\s\S]*?)</title>", block)
        if tm:
            title = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", tm.group(1)).strip()
        lm = re.search(r"<link[^>]*>([\s\S]*?)</link>", block)
        if lm:
            link = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", lm.group(1)).strip()
        else:
            lm2 = re.search(r'<link[^>]*href="([^"]+)"', block)
            if lm2:
                link = lm2.group(1).strip()
        dm = re.search(r"<(?:pubDate|published|updated)[^>]*>([\s\S]*?)</", block)
        if dm:
            pub_date = dm.group(1).strip()
        sm = re.search(r"<(?:description|summary|content)[^>]*>([\s\S]*?)</", block)
        if sm:
            summary = re.sub(r"<[^>]+>", "", re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", sm.group(1))).strip()[:500]
        if title and link:
            items.append({"title": title, "link": link, "pub_date": pub_date, "summary": summary})
    return items


def fetch_url(url: str) -> str:
    req = Request(url, headers={"User-Agent": "TENET5-liril-hears/1.0 (tenet-5.github.io)"})
    with urlopen(req, timeout=HTTP_TIMEOUT) as r:
        data = r.read()
    # Try UTF-8 first; fall back to latin-1
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    # Cap size — keep most recent 5000 hashes
    if len(seen) > 5000:
        seen = set(list(seen)[-5000:])
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


async def publish(nc, subject: str, payload: dict) -> None:
    try:
        await nc.publish(subject, json.dumps(payload).encode("utf-8"))
    except Exception:
        with QUEUE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"subject": subject, **payload}, ensure_ascii=False) + "\n")


async def poll_feed(nc, leaf: str, url: str, notes: str, seen: set[str]) -> int:
    try:
        xml = fetch_url(url)
    except Exception as e:
        _log("FETCH", f"{url} failed: {e!r}")
        return 0
    items = parse_rss(xml)
    new = 0
    for it in items:
        h = item_hash(it["link"], it["title"])
        if h in seen:
            continue
        seen.add(h)
        event = {
            "ts":       int(time.time()),
            "source":   notes,
            "feed_url": url,
            "title":    it["title"],
            "link":     it["link"],
            "pub_date": it["pub_date"],
            "summary":  it["summary"],
            "hash":     h,
        }
        await publish(nc, f"tenet5.liril.hear.{leaf}", event)
        new += 1
    return new


async def poll_loop(nc) -> None:
    seen = load_seen()
    _log("BOOT", f"starting poll loop; {len(seen)} items previously seen")
    while True:
        for leaf, url, notes in FEEDS:
            new = await poll_feed(nc, leaf, url, notes, seen)
            if new:
                _log("POLL", f"{notes}: {new} new item(s)")
        save_seen(seen)
        await asyncio.sleep(FEED_POLL_SECONDS)


async def main() -> None:
    _log("BOOT", f"NATS_URL={os.environ['NATS_URL']}  {len(FEEDS)} feeds")
    while True:
        try:
            nc = await nats.connect(os.environ["NATS_URL"], connect_timeout=5, reconnect_time_wait=2, max_reconnect_attempts=-1)
            await poll_loop(nc)
        except Exception as e:
            _log("ERROR", f"top-level {e!r}; sleep 10 and reconnect")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
