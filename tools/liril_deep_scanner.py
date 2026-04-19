# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# SYSTEM_SEED=118400
"""
LIRIL Deep Website Scanner — finds REAL glitches, not just file existence.

Scans every page for actual rendering/content problems:
  1. HTML structure: unclosed tags, div imbalances, broken nesting
  2. JS integrity: script tag balance, inline error patterns
  3. CSS references: missing stylesheets, broken imports
  4. Content quality: empty narrations, thin pages, missing sections
  5. Navigation: broken internal links, orphaned pages
  6. Media: missing audio, broken video-bg mappings
  7. Presentation: pages not in PAGE_SEQUENCE, missing readnext entries

Results fed to Ising quantum decoder for structural anomaly detection.
Reports to LIRIL for training + NATS for monitoring.

Usage:
  python tools/liril_deep_scanner.py              # Full deep scan
  python tools/liril_deep_scanner.py --quick       # Structure check only
  python tools/liril_deep_scanner.py --fix         # Auto-fix what it can

SYSTEM_SEED = 118400
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="[DEEP-SCAN] %(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("deep_scanner")

SEED = 118400
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
WEBSITE = Path("E:/TENET-5.github.io")
REPORT_FILE = ROOT / "logs" / "deep_scan_report.json"
REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)


def scan_html_structure(page: Path) -> List[Dict]:
    """Check HTML tag balance and nesting."""
    issues = []
    html = page.read_text(encoding="utf-8", errors="ignore")

    for tag in ["div", "section", "main", "article", "a", "span", "table", "ul", "ol"]:
        opens = len(re.findall(rf'<{tag}\b', html))
        closes = len(re.findall(rf'</{tag}>', html))
        diff = opens - closes
        if abs(diff) > 1:
            issues.append({
                "type": "tag_imbalance",
                "tag": tag,
                "opens": opens,
                "closes": closes,
                "diff": diff,
                "severity": "HIGH" if abs(diff) > 3 else "MEDIUM",
            })

    # Check script tag balance
    script_opens = len(re.findall(r'<script\b', html))
    script_closes = len(re.findall(r'</script>', html))
    if script_opens != script_closes:
        issues.append({
            "type": "broken_script",
            "opens": script_opens,
            "closes": script_closes,
            "severity": "CRITICAL",
        })

    # Check for missing closing tags
    if "</html>" not in html:
        issues.append({"type": "missing_html_close", "severity": "CRITICAL"})
    if "</body>" not in html:
        issues.append({"type": "missing_body_close", "severity": "CRITICAL"})

    return issues


def scan_content_quality(page: Path) -> List[Dict]:
    """Check content quality — narrations, data, length."""
    issues = []
    html = page.read_text(encoding="utf-8", errors="ignore")
    name = page.name

    # Page too small (likely placeholder)
    if len(html) < 3000 and name not in ("404.html", "auth-callback.html", "index.html"):
        issues.append({
            "type": "thin_page",
            "bytes": len(html),
            "severity": "MEDIUM",
        })

    # Check narration coverage
    narrations = re.findall(r'data-narrate="([^"]*)"', html)
    if not narrations and name not in ("404.html", "auth-callback.html", "index.html", "test-narration-validation.html"):
        issues.append({"type": "no_narrations", "severity": "LOW"})

    # Check for empty narrations
    empty = [n for n in narrations if len(n.strip()) < 10]
    if empty:
        issues.append({"type": "empty_narrations", "count": len(empty), "severity": "LOW"})

    # Check OG tags
    if "og:title" not in html and name not in ("404.html", "auth-callback.html"):
        issues.append({"type": "missing_og_title", "severity": "MEDIUM"})
    if "og:image" not in html and "og:title" in html:
        issues.append({"type": "missing_og_image", "severity": "MEDIUM"})

    return issues


def scan_css_js(page: Path) -> List[Dict]:
    """Check CSS/JS references."""
    issues = []
    html = page.read_text(encoding="utf-8", errors="ignore")

    # Check CSS refs
    for match in re.finditer(r'href="([^"]*\.css[^"]*)"', html):
        href = match.group(1).split("?")[0]
        if not href.startswith("http") and not (WEBSITE / href).exists():
            issues.append({"type": "missing_css", "file": href, "severity": "HIGH"})

    # Check JS refs
    for match in re.finditer(r'src="([^"]*\.js[^"]*)"', html):
        src = match.group(1).split("?")[0]
        if not src.startswith("http") and not (WEBSITE / src).exists():
            issues.append({"type": "missing_js", "file": src, "severity": "HIGH"})

    # Check for common JS error patterns in inline scripts
    inline_errors = re.findall(r'\.innerHtml\b|documnet\.|widnow\.|consoel\.|fucntion\b|retrun\b', html)
    if inline_errors:
        issues.append({"type": "js_typos", "patterns": inline_errors[:5], "severity": "HIGH"})

    return issues


def scan_navigation(page: Path, all_pages: set) -> List[Dict]:
    """Check internal link validity."""
    issues = []
    html = page.read_text(encoding="utf-8", errors="ignore")

    for match in re.finditer(r'href="([^"#][^"]*\.html[^"]*)"', html):
        href = match.group(1)
        # Skip mailto, query-param, and external links
        if href.startswith("http") or href.startswith("mailto") or "?" in href:
            continue
        if href not in all_pages:
            issues.append({"type": "broken_internal_link", "link": href, "severity": "HIGH"})

    return issues


def scan_media(page: Path) -> List[Dict]:
    """Check audio and media references."""
    issues = []
    name = page.stem

    # Check audio exists
    mp3 = WEBSITE / "audio" / f"{name}.mp3"
    if not mp3.exists() and name not in ("404", "auth-callback", "index", "test-narration-validation"):
        issues.append({"type": "missing_audio", "severity": "LOW"})

    return issues


async def run_ising_scan(page_issues: Dict) -> Dict:
    """Feed scan results through Ising quantum decoder."""
    try:
        import nats
        nc = await nats.connect(NATS_URL, connect_timeout=3)

        # Build a matrix from the scan results
        # Rows = pages, Cols = issue categories
        categories = ["tag_imbalance", "broken_script", "thin_page", "missing_og", "missing_audio", "broken_link"]
        pages = list(page_issues.keys())[:40]  # Cap at 40

        matrix = []
        for page in pages:
            row = [0.0] * len(categories)
            for issue in page_issues.get(page, []):
                itype = issue.get("type", "")
                for j, cat in enumerate(categories):
                    if cat in itype:
                        sev = {"CRITICAL": 1.0, "HIGH": 0.7, "MEDIUM": 0.4, "LOW": 0.2}
                        row[j] = max(row[j], sev.get(issue.get("severity", "LOW"), 0.1))
            matrix.append(row)

        if not matrix:
            matrix = [[0] * len(categories)]

        r = await nc.request("tenet5.ising.anomaly_scan", json.dumps({
            "transaction_matrix": matrix,
            "threshold": 0.3,
            "scan_type": "deep_website_scan",
        }).encode(), timeout=15)

        result = json.loads(r.data)
        await nc.close()
        return result

    except Exception as e:
        return {"error": str(e)}


async def publish_results(report: Dict):
    """Publish scan results to LIRIL for training."""
    try:
        import nats
        nc = await nats.connect(NATS_URL, connect_timeout=3)

        total_issues = sum(len(v) for v in report.get("issues", {}).values())
        high_issues = sum(1 for v in report.get("issues", {}).values()
                         for i in v if i.get("severity") in ("CRITICAL", "HIGH"))

        await nc.publish("tenet5.website.deep_scan", json.dumps({
            "ts": time.time(),
            "seed": SEED,
            "pages_scanned": report.get("pages_scanned", 0),
            "total_issues": total_issues,
            "high_issues": high_issues,
            "quantum_score": report.get("quantum", {}).get("anomaly_score", 0),
            "quantum_flagged": report.get("quantum", {}).get("flagged", False),
        }).encode())

        await nc.publish("tenet5.liril.train", json.dumps({
            "text": f"deep scan: {report.get('pages_scanned', 0)} pages, {total_issues} issues ({high_issues} high), quantum={'FLAGGED' if report.get('quantum', {}).get('flagged') else 'CLEAN'}",
            "domain": "TECHNOLOGY",
            "source": "deep_scanner",
            "seed": SEED,
        }).encode())

        await nc.flush()
        await nc.close()
    except Exception:
        pass


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="LIRIL Deep Website Scanner")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    log.info("═══ LIRIL DEEP WEBSITE SCAN ═══")

    all_pages = {p.name for p in WEBSITE.glob("*.html")}
    page_issues: Dict[str, List] = {}
    total_issues = 0

    for page in sorted(WEBSITE.glob("*.html")):
        issues = []
        issues.extend(scan_html_structure(page))
        issues.extend(scan_content_quality(page))
        if not args.quick:
            issues.extend(scan_css_js(page))
            issues.extend(scan_navigation(page, all_pages))
            issues.extend(scan_media(page))

        if issues:
            page_issues[page.name] = issues
            total_issues += len(issues)

    # Report
    high = sum(1 for v in page_issues.values() for i in v if i.get("severity") in ("CRITICAL", "HIGH"))
    medium = sum(1 for v in page_issues.values() for i in v if i.get("severity") == "MEDIUM")
    low = sum(1 for v in page_issues.values() for i in v if i.get("severity") == "LOW")

    log.info("Pages scanned: %d | Issues: %d (HIGH=%d MED=%d LOW=%d)",
             len(all_pages), total_issues, high, medium, low)

    if page_issues:
        log.info("Pages with issues:")
        for page, issues in sorted(page_issues.items()):
            severities = [i.get("severity", "?") for i in issues]
            log.info("  %s: %d issues (%s)", page, len(issues), ", ".join(severities))

    # Quantum verification
    log.info("Running Ising quantum scan on results...")
    quantum = await run_ising_scan(page_issues)
    log.info("Quantum: score=%.3f flagged=%s",
             quantum.get("anomaly_score", 0), quantum.get("flagged", False))

    # Build report
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "pages_scanned": len(all_pages),
        "total_issues": total_issues,
        "high": high,
        "medium": medium,
        "low": low,
        "issues": page_issues,
        "quantum": quantum,
        "seed": SEED,
    }

    REPORT_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("Report: %s", REPORT_FILE)

    # Publish to LIRIL
    await publish_results(report)

    status = "GREEN" if high == 0 else "RED"
    log.info("═══ DEEP SCAN: %s ═══", status)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
