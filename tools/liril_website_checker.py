# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# SYSTEM_SEED=118400

# ── GAME_MODE GUARD ─────────────────────────────────────────────────────
# If data/.game_mode sentinel exists, exit immediately. Task Scheduler
# may still invoke this script on its timer, but we bail in ~5ms before
# launching HTTP requests or opening a console window long enough to
# steal focus. Delete data/.game_mode to re-enable.
import os as _os, sys as _sys
if _os.path.exists(r"E:\S.L.A.T.E\tenet5\data\.game_mode"):
    _sys.exit(0)
# ────────────────────────────────────────────────────────────────────────

"""
LIRIL Website Quality Checker — GPU-powered page verification.

Uses DEIFIED cascade inference to verify website page quality:
  1. Checks all pages return HTTP 200
  2. Verifies OG tags, audio, video-bg mapping
  3. Detects wrong domain (tenet5 vs tenet-5)
  4. Detects NemoClaw telemetry injection
  5. Reports to LIRIL via NATS

Usage:
  python tools/liril_website_checker.py              # Full check
  python tools/liril_website_checker.py --quick       # HTTP-only check
  python tools/liril_website_checker.py --report      # Generate report

SYSTEM_SEED = 118400
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="[CHECKER] %(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("checker")
logging.getLogger("nats").setLevel(logging.CRITICAL)

SEED = 118400
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
WEBSITE = Path("E:/TENET-5.github.io")
SITE_URL = "https://tenet-5.github.io"
REPORT_FILE = ROOT / "logs" / "website_quality_report.json"
PUBLIC_REPORT_FILE = WEBSITE / "data" / "website_quality_status.json"
PUBLIC_QUANTUM_FILE = WEBSITE / "data" / "abcxyz_quantum_status.json"
HTTP_TIMEOUT = 5
REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
PUBLIC_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)


def _uniq(items: List[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


NATS_CANDIDATES = _uniq([
    os.environ.get("NATS_URL_HOST"),
    os.environ.get("TENET_NATS_URL"),
    NATS_URL,
    "nats://127.0.0.1:4223",
    "nats://127.0.0.1:4223",
    "nats://127.0.0.1:14222",
])


def check_files():
    """Check local file integrity."""
    results = {"pages": 0, "audio": 0, "video_bg": 0, "og_images": 0, "issues": []}

    # Count pages
    pages = list(WEBSITE.glob("*.html"))
    results["pages"] = len(pages)

    # Count audio
    audio = list((WEBSITE / "audio").glob("*.mp3"))
    results["audio"] = len(audio)

    # Count video backgrounds
    vids = list((WEBSITE / "media" / "backgrounds").glob("*.mp4"))
    results["video_bg"] = len(vids)

    # Check wrong domain
    for page in pages:
        content = page.read_text(encoding="utf-8", errors="ignore")
        if "tenet5.github.io" in content and "tenet-5.github.io" not in content:
            results["issues"].append(f"WRONG_DOMAIN: {page.name}")

    # Check NemoClaw telemetry
    for page in pages:
        content = page.read_text(encoding="utf-8", errors="ignore")
        if "Vulnerability Endpoint Locked" in content or "Sync Target" in content:
            results["issues"].append(f"NEMOCLAW_TELEMETRY: {page.name}")

    # Check OG images
    pages_without_og = []
    for page in pages:
        content = page.read_text(encoding="utf-8", errors="ignore")
        if "og:image" not in content and "og:title" in content:
            pages_without_og.append(page.name)
    if pages_without_og:
        results["issues"].append(f"MISSING_OG_IMAGE: {len(pages_without_og)} pages")
    results["og_images"] = results["pages"] - len(pages_without_og)

    # Check shell.js version consistency
    versions = {}
    for page in pages:
        content = page.read_text(encoding="utf-8", errors="ignore")
        import re
        m = re.search(r'shell\.js\?v=(\d+)', content)
        if m:
            v = m.group(1)
            versions[v] = versions.get(v, 0) + 1
    if len(versions) > 1:
        results["issues"].append(f"SHELL_VERSION_MISMATCH: {versions}")
    results["shell_versions"] = versions

    # Check audio coverage
    audio_names = {a.stem for a in audio}
    page_names = {p.stem for p in pages}
    # liril-live is a live dashboard, not a narrative page — narration delivered via NATS not static mp3
    missing_audio = page_names - audio_names - {"404", "auth-callback", "index", "test-narration-validation", "liril-live"}
    if missing_audio:
        results["issues"].append(f"MISSING_AUDIO: {sorted(missing_audio)}")

    return results


def check_http(quick=False):
    """Check HTTP status of key pages."""
    import urllib.request
    import urllib.error

    test_pages = ["index.html", "home.html", "panama-papers.html", "findings.html", "search.html"]

    if not quick:
        test_pages = [p.name for p in WEBSITE.glob("*.html")]

    results = {"total": len(test_pages), "ok": 0, "failed": 0, "failures": []}
    headers = {"User-Agent": "TENET5-WebsiteChecker/1.0"}

    for page in test_pages[:20]:
        url = f"{SITE_URL}/{page}"
        try:
            req = urllib.request.Request(url, headers=headers, method="HEAD")
            resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
            if getattr(resp, "status", 200) == 200:
                results["ok"] += 1
            else:
                results["failures"].append(f"{page}: {resp.status}")
                results["failed"] += 1
        except urllib.error.HTTPError as e:
            if e.code in {403, 405}:
                try:
                    req = urllib.request.Request(url, headers=headers, method="GET")
                    resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
                    if getattr(resp, "status", 200) == 200:
                        results["ok"] += 1
                    else:
                        results["failures"].append(f"{page}: {resp.status}")
                        results["failed"] += 1
                except Exception as inner:
                    results["failures"].append(f"{page}: {inner}")
                    results["failed"] += 1
            else:
                results["failures"].append(f"{page}: HTTP {e.code}")
                results["failed"] += 1
        except Exception as e:
            results["failures"].append(f"{page}: {e}")
            results["failed"] += 1

    return results


async def _connect_nats():
    import nats

    last_error = None
    for server in NATS_CANDIDATES:
        try:
            return await nats.connect(
                servers=[server],
                connect_timeout=1,
                max_reconnect_attempts=0,
                allow_reconnect=False,
                name="website-checker",
            )
        except Exception as e:
            last_error = e
    raise RuntimeError(f"No reachable NATS server: {last_error}")


async def report_to_liril(file_results, http_results):
    """Send quality report to LIRIL via NATS."""
    try:
        nc = await asyncio.wait_for(_connect_nats(), timeout=4)

        status = "GREEN" if not file_results["issues"] and http_results["failed"] == 0 else "RED"
        report = {
            "ts": time.time(),
            "seed": SEED,
            "status": status,
            "pages": file_results["pages"],
            "audio": file_results["audio"],
            "video_bg": file_results["video_bg"],
            "og_images": file_results["og_images"],
            "issues": file_results["issues"],
            "http_ok": http_results["ok"],
            "http_failed": http_results["failed"],
            "http_failures": http_results["failures"],
            "checker": "liril_website_checker",
        }

        await nc.publish("tenet5.website.verification", json.dumps(report).encode())

        issue_count = len(file_results["issues"]) + http_results["failed"]
        await nc.publish("tenet5.liril.train", json.dumps({
            "text": f"website quality check: {status}, {file_results['pages']} pages, "
                    f"{file_results['audio']} audio, {issue_count} issues",
            "domain": "TECHNOLOGY",
            "source": "website_checker",
            "seed": SEED,
        }).encode())

        await asyncio.wait_for(nc.flush(), timeout=2)
        await asyncio.wait_for(nc.close(), timeout=2)
        log.info("Report sent to LIRIL: %s", status)
    except Exception as e:
        log.warning("NATS report skipped: %s", e)


def save_report(file_results, http_results):
    """Save quality report to disk and export a public site-health summary."""
    status = "GREEN" if not file_results["issues"] and http_results["failed"] == 0 else "RED"
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "seed": SEED,
        "files": file_results,
        "http": http_results,
        "status": status,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    public_report = {
        "generated": report["generated"],
        "status": status,
        "pages": file_results["pages"],
        "audio": file_results["audio"],
        "video_bg": file_results["video_bg"],
        "issues": len(file_results["issues"]),
        "http_ok": http_results["ok"],
        "http_failed": http_results["failed"],
        "top_issues": file_results["issues"][:10],
        "seed": SEED,
    }
    PUBLIC_REPORT_FILE.write_text(json.dumps(public_report, indent=2, default=str), encoding="utf-8")

    quantum_report = {
        "generated": report["generated"],
        "seed": SEED,
        "ok": False,
        "status": "UNKNOWN",
    }
    try:
        from tenet.liril_agentic import LirilTools

        live_quantum = LirilTools.abcxyz_quantum_analysis()
        quantum_report.update(live_quantum)
        quantum_report["status"] = "GREEN" if live_quantum.get("ok") else "WARN"
    except Exception as e:
        quantum_report["error"] = str(e)
        quantum_report["status"] = "WARN"

    PUBLIC_QUANTUM_FILE.write_text(json.dumps(quantum_report, indent=2, default=str), encoding="utf-8")
    log.info("Report saved: %s", REPORT_FILE)
    log.info("Public site status saved: %s", PUBLIC_REPORT_FILE)
    log.info("Public quantum status saved: %s", PUBLIC_QUANTUM_FILE)


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="LIRIL Website Quality Checker")
    parser.add_argument("--quick", action="store_true", help="HTTP check key pages only")
    parser.add_argument("--report", action="store_true", help="Show last report")
    args = parser.parse_args()

    if args.report:
        if REPORT_FILE.exists():
            print(REPORT_FILE.read_text(encoding="utf-8"))
        else:
            print("No report yet")
        return

    log.info("═══ LIRIL WEBSITE QUALITY CHECK ═══")

    # File checks
    file_results = check_files()
    log.info("Pages: %d | Audio: %d | Video: %d | OG: %d | Issues: %d",
             file_results["pages"], file_results["audio"],
             file_results["video_bg"], file_results["og_images"],
             len(file_results["issues"]))

    if file_results["issues"]:
        for issue in file_results["issues"]:
            log.warning("  ⚠ %s", issue)

    # HTTP checks
    http_results = check_http(quick=args.quick)
    log.info("HTTP: %d/%d OK, %d failed",
             http_results["ok"], http_results["total"], http_results["failed"])

    # Report to LIRIL
    await report_to_liril(file_results, http_results)

    # Save report
    save_report(file_results, http_results)

    status = "GREEN" if not file_results["issues"] and http_results["failed"] == 0 else "RED"
    log.info("═══ STATUS: %s ═══", status)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
                if policy_cls is not None:
                    asyncio.set_event_loop_policy(policy_cls())
        except Exception:
            pass
    asyncio.run(main())
