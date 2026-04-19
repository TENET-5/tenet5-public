"""
LIRIL Walkthrough Test
======================
AI-driven end-to-end website verification. LIRIL drives a real Chromium
browser through every page in PAGE_SEQUENCE and:

  1. Verifies the cross-page LIRIL walkthrough handoff actually fires
     from home.html (sets liril_autopilot, posts pres-navigate).
  2. For every page in the canonical reading order:
       - HTTP status / load OK
       - JavaScript console errors / page errors
       - data-narrate text length (LIRIL must have something to read)
       - presence of <audio> per page (narration mp3)
       - footer + header frames mounted
       - presentation engine loaded (__TENET5_PRESENTATION_LOADED)
       - liril-walkthrough.js installed bridge button or skip
       - 404 / failed network requests
  3. Writes a JSON report and a public status JSON, then trains LIRIL.

Usage:
    .venv\\Scripts\\python.exe tools\\liril_walkthrough_test.py [--quick] [--max N]

  --quick : only test a sampled subset (10 pages: head/middle/tail)
  --max N : cap to first N pages of PAGE_SEQUENCE
"""
from __future__ import annotations
import argparse
import http.server
import json
import re
import socketserver
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE_ROOT = Path(r"E:\TENET-5.github.io")
TOOLS_ROOT = Path(r"E:\S.L.A.T.E\tenet5\tools")
LOGS_ROOT = Path(r"E:\S.L.A.T.E\tenet5\logs")
LOGS_ROOT.mkdir(parents=True, exist_ok=True)

REPORT_PATH = LOGS_ROOT / "liril_walkthrough_test_report.json"
PUBLIC_STATUS = SITE_ROOT / "data" / "liril_walkthrough_test_status.json"

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"

# ───────────────────────────── PAGE_SEQUENCE extraction ─────────────────────────────

def load_page_sequence() -> list[str]:
    """Parse PAGE_SEQUENCE array out of presentation.js."""
    src = (SITE_ROOT / "js" / "presentation.js").read_text(encoding="utf-8")
    m = re.search(r"var\s+PAGE_SEQUENCE\s*=\s*\[(.*?)\];", src, re.DOTALL)
    if not m:
        raise RuntimeError("Could not locate PAGE_SEQUENCE in presentation.js")
    body = m.group(1)
    pages = re.findall(r"'([^']+\.html)'", body)
    return pages


# ───────────────────────────── Local static server ─────────────────────────────

class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):  # silence
        pass


def start_server() -> socketserver.TCPServer:
    handler = lambda *a, **k: _Handler(*a, directory=str(SITE_ROOT), **k)
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ───────────────────────────── Per-page audit ─────────────────────────────

PAGE_AUDIT_JS = r"""
() => {
  const out = {
    title: document.title || '',
    h1: (document.querySelector('h1')?.innerText || '').slice(0, 200),
    narrateNodes: document.querySelectorAll('[data-narrate]').length,
    narrateTotalLen: Array.from(document.querySelectorAll('[data-narrate]'))
                      .reduce((a, e) => a + (e.getAttribute('data-narrate') || '').length, 0),
    audioCount: document.querySelectorAll('audio').length,
    audioSrcs: Array.from(document.querySelectorAll('audio source, audio'))
                  .map(e => e.src || e.getAttribute('src') || '').filter(Boolean).slice(0, 8),
    headerMounted: !!document.querySelector('#site-header-frame *, header'),
    footerMounted: !!document.querySelector('#site-footer-frame *, footer'),
    presentationLoaded: !!window.__TENET5_PRESENTATION_LOADED,
    narrateAllExposed: typeof window.__TENET5_LIRIL_NARRATE_ALL === 'function',
    nextPageExposed: typeof window.__TENET5_NEXT_PAGE === 'function',
    walkthroughBridge: !!document.querySelector('.liril-walkthrough-bridge, #liril-bridge-btn, [data-liril-bridge]'),
    hasMetaDescription: !!document.querySelector('meta[name="description"]'),
    hasOgImage: !!document.querySelector('meta[property="og:image"]'),
    bodyLen: (document.body?.innerText || '').length,
  };
  return out;
}
"""

REQUIRED_FOR_NARRATION = ("narrateTotalLen",)  # must be > 0


_IGNORE_CONSOLE = (
    "frame-ancestors",            # CSP meta warning, harmless
    "Failed to load resource",    # subsumed by requestfailed
)
_IGNORE_REQ = (
    "favicon", "data:", "supabase", "cdn.jsdelivr.net",
    "google-analytics", "googletagmanager",
)


def audit_page(page, slug: str, timeout_ms: int = 20000) -> dict:
    """Load `index.html?load={slug}` (the shell+iframe), then audit the iframe content."""
    errs: list[str] = []
    failed_reqs: list[dict] = []

    def on_console(msg):
        if msg.type != "error":
            return
        text = msg.text[:300]
        if any(s in text for s in _IGNORE_CONSOLE):
            return
        errs.append(f"[console.error] {text}")

    def on_pageerror(exc):
        errs.append(f"[pageerror] {str(exc)[:300]}")

    def on_requestfailed(req):
        if any(s in req.url for s in _IGNORE_REQ):
            return
        # ERR_ABORTED happens when the iframe navigates and cancels in-flight
        # requests (preloaded audio, fonts, etc). These are not real failures.
        fail = req.failure or ""
        if "ERR_ABORTED" in fail:
            return
        failed_reqs.append({"url": req.url, "failure": fail or "unknown"})

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)

    shell_url = f"{BASE_URL}/index.html?load={slug}"
    t0 = time.time()
    status = None
    try:
        resp = page.goto(shell_url, wait_until="domcontentloaded", timeout=timeout_ms)
        status = resp.status if resp else None
        # Wait for the iframe to swap to the requested page and settle
        page.wait_for_function(
            f"() => {{ try {{ var f=document.getElementById('content_frame'); "
            f"return f && f.contentWindow && f.contentWindow.location.pathname.endsWith('/{slug}') "
            f"&& f.contentWindow.document.readyState === 'complete'; }} catch(e) {{ return false; }} }}",
            timeout=15000,
        )
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
    except Exception as e:
        errs.append(f"[goto] {str(e)[:300]}")

    info = {}
    try:
        frame = next((f for f in page.frames if f.name == "content_frame"
                      or f.url.endswith("/" + slug)), None)
        if frame is None:
            errs.append(f"[frame] content_frame not found for {slug}")
        else:
            info = frame.evaluate(PAGE_AUDIT_JS)
    except Exception as e:
        errs.append(f"[evaluate] {str(e)[:300]}")

    elapsed = round(time.time() - t0, 2)

    page.remove_listener("console", on_console)
    page.remove_listener("pageerror", on_pageerror)
    page.remove_listener("requestfailed", on_requestfailed)

    return {
        "url": shell_url,
        "slug": slug,
        "http_status": status,
        "load_seconds": elapsed,
        "errors": errs,
        "failed_requests": failed_reqs[:20],
        **info,
    }


# ───────────────────────────── Walkthrough handoff test ─────────────────────────────

def test_handoff(page, log: list) -> dict:
    """Verify clicking the home walkthrough button sets autopilot flag in the iframe."""
    shell_url = f"{BASE_URL}/index.html?load=home.html"
    page.goto(shell_url, wait_until="domcontentloaded", timeout=20000)
    try:
        page.wait_for_function(
            "() => { try { var f=document.getElementById('content_frame'); "
            "return f && f.contentWindow && f.contentWindow.location.pathname.endsWith('/home.html') "
            "&& f.contentWindow.document.readyState === 'complete'; } catch(e) { return false; } }",
            timeout=15000,
        )
    except Exception as e:
        log.append(f"home iframe load wait error: {e}")
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass

    frame = next((f for f in page.frames if f.name == "content_frame"
                  or f.url.endswith("/home.html")), None)
    if frame is None:
        log.append("content_frame not found for home.html")
        return {"fullsite_button_present": False, "autopilot_set_by_click": False,
                "engine_inspect": {"error": "no_frame"}}

    has_btn = frame.evaluate("() => !!document.getElementById('btn-liril-fullsite')")
    log.append(f"home.html #btn-liril-fullsite present: {has_btn}")

    # Wait for engine to be initialized AND the DOMContentLoaded handler to have attached
    # the button click listener. engine.scenes is populated by engine.init() inside
    # DOMContentLoaded, so it's a reliable readiness gate.
    try:
        frame.wait_for_function(
            "() => !!window.engine && typeof window.engine.startJourney === 'function' "
            "&& window.engine.scenes && window.engine.scenes.length > 0",
            timeout=10000,
        )
    except Exception as e:
        log.append(f"engine wait error: {e}")

    autopilot_before = frame.evaluate("() => sessionStorage.getItem('liril_autopilot')")

    if has_btn:
        try:
            # Use direct DOM .click() to bypass any visibility/occlusion guards.
            # The handler is a synchronous click event listener in home.html that sets
            # sessionStorage.liril_autopilot before doing anything else.
            frame.evaluate("() => { var b = document.getElementById('btn-liril-fullsite'); if (b) b.click(); }")
            page.wait_for_timeout(800)
        except Exception as e:
            log.append(f"click error: {e}")

    autopilot_after = frame.evaluate("() => sessionStorage.getItem('liril_autopilot')")

    nextscene_check = frame.evaluate(
        r"""() => {
            try {
                const src = (window.engine && window.engine.nextScene)
                              ? window.engine.nextScene.toString() : '';
                return {
                    engine: !!window.engine,
                    sceneOrderLen: (window.engine && window.engine.sceneOrder) ? window.engine.sceneOrder.length : 0,
                    handoffWired: src.includes('liril_autopilot') && (src.includes('pres-navigate') || src.includes('complete-thesis')),
                };
            } catch(e) { return {error: String(e)}; }
        }"""
    )

    return {
        "fullsite_button_present": has_btn,
        "autopilot_before_click": autopilot_before,
        "autopilot_after_click": autopilot_after,
        "autopilot_set_by_click": bool(autopilot_after) and autopilot_before != autopilot_after,
        "engine_inspect": nextscene_check,
    }


# ───────────────────────────── Problem classification ─────────────────────────────

# Internal/operational tool pages — exempt from narration depth checks.
NARRATION_EXEMPT = {
    "campaign-generator.html",
}


def classify_problems(page_results: list[dict]) -> dict:
    problems = []
    for r in page_results:
        slug = r.get("slug") or r.get("url", "").rsplit("/", 1)[-1]
        if r.get("http_status") and r["http_status"] >= 400:
            problems.append({"page": slug, "severity": "high", "kind": "http_error", "detail": f"status {r['http_status']}"})
        for e in r.get("errors", []):
            sev = "high" if "[pageerror]" in e else "medium"
            problems.append({"page": slug, "severity": sev, "kind": "js_error", "detail": e})
        for fr in r.get("failed_requests", []):
            problems.append({"page": slug, "severity": "medium", "kind": "failed_request", "detail": fr["url"]})
        if r.get("narrateTotalLen", 0) < 100 and slug not in NARRATION_EXEMPT:
            problems.append({"page": slug, "severity": "medium", "kind": "thin_narration",
                             "detail": f"data-narrate total len = {r.get('narrateTotalLen', 0)}"})
        # Note: site uses dynamic `new Audio(mp3)` in liril-walkthrough.js, not <audio> tags.
        # The presence/absence of <audio> elements is not a real problem signal.
        if not r.get("presentationLoaded"):
            problems.append({"page": slug, "severity": "low", "kind": "presentation_missing",
                             "detail": "__TENET5_PRESENTATION_LOADED is false"})
        if not r.get("hasMetaDescription"):
            problems.append({"page": slug, "severity": "low", "kind": "no_meta_desc",
                             "detail": "missing meta description"})
        if r.get("bodyLen", 0) < 500:
            problems.append({"page": slug, "severity": "high", "kind": "empty_page",
                             "detail": f"body innerText only {r.get('bodyLen', 0)} chars"})

    counts = {}
    for p in problems:
        counts[p["kind"]] = counts.get(p["kind"], 0) + 1

    return {"problem_count": len(problems), "by_kind": counts, "problems": problems}


# ───────────────────────────── Main ─────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="sample 10 pages")
    ap.add_argument("--max", type=int, default=0, help="cap pages")
    ap.add_argument("--headed", action="store_true", help="visible browser")
    args = ap.parse_args()

    pages = load_page_sequence()
    if args.quick:
        n = len(pages)
        sample_idx = sorted(set([0, 1, 2, n // 4, n // 3, n // 2, (2 * n) // 3, (3 * n) // 4, n - 2, n - 1]))
        pages = [pages[i] for i in sample_idx]
    elif args.max > 0:
        pages = pages[: args.max]

    print(f"[LIRIL-TEST] Pages to audit: {len(pages)}")
    print(f"[LIRIL-TEST] Starting local server :{PORT} on {SITE_ROOT}")
    httpd = start_server()
    # give server a beat
    time.sleep(0.5)

    log: list[str] = []
    page_results: list[dict] = []
    handoff_result: dict = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(viewport={"width": 1366, "height": 900})
            page = ctx.new_page()

            print("[LIRIL-TEST] Testing walkthrough handoff on home.html ...")
            handoff_result = test_handoff(page, log)
            print(f"[LIRIL-TEST]   -> fullsite_button={handoff_result.get('fullsite_button_present')} "
                  f"autopilot_set={handoff_result.get('autopilot_set_by_click')} "
                  f"handoff_wired={handoff_result.get('engine_inspect', {}).get('handoffWired')}")

            for i, slug in enumerate(pages, 1):
                # clear sessionStorage between pages so audits are independent
                try:
                    page.evaluate("() => { try { sessionStorage.clear(); } catch(e){} }")
                except Exception:
                    pass
                r = audit_page(page, slug)
                page_results.append(r)
                err_cnt = len(r.get("errors", []))
                badge = "OK" if err_cnt == 0 and (r.get("http_status") or 0) < 400 else "ISSUE"
                print(f"[LIRIL-TEST] {i:>3}/{len(pages)} [{badge}] {slug:<42} "
                      f"status={r.get('http_status')} errs={err_cnt} "
                      f"narrate={r.get('narrateTotalLen', 0)} audio={r.get('audioCount', 0)} "
                      f"t={r.get('load_seconds')}s")

            ctx.close()
            browser.close()
    finally:
        httpd.shutdown()

    classification = classify_problems(page_results)

    summary = {
        "ok_pages": sum(1 for r in page_results
                        if not r.get("errors") and (r.get("http_status") or 0) < 400),
        "total_pages": len(page_results),
        "total_js_errors": sum(len(r.get("errors", [])) for r in page_results),
        "total_failed_requests": sum(len(r.get("failed_requests", [])) for r in page_results),
        "avg_load_seconds": round(
            sum(r.get("load_seconds", 0) for r in page_results) / max(1, len(page_results)), 2
        ),
    }

    status = "GREEN"
    if classification["problem_count"] > 0:
        high = sum(1 for p in classification["problems"] if p["severity"] == "high")
        status = "RED" if high > 0 else "AMBER"
    if not handoff_result.get("autopilot_set_by_click"):
        status = "RED"

    report = {
        "timestamp_utc": datetime.now(tz=__import__('datetime').timezone.utc).isoformat(),
        "status": status,
        "summary": summary,
        "handoff": handoff_result,
        "classification": classification,
        "log": log,
        "pages": page_results,
    }

    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    PUBLIC_STATUS.parent.mkdir(parents=True, exist_ok=True)
    public = {
        "timestamp_utc": report["timestamp_utc"],
        "status": status,
        "summary": summary,
        "handoff_ok": bool(handoff_result.get("autopilot_set_by_click")),
        "problem_count": classification["problem_count"],
        "problems_by_kind": classification["by_kind"],
        "top_problems": classification["problems"][:25],
    }
    PUBLIC_STATUS.write_text(json.dumps(public, indent=2), encoding="utf-8")

    print()
    print(f"[LIRIL-TEST] ═══ STATUS: {status} ═══")
    print(f"[LIRIL-TEST] OK: {summary['ok_pages']}/{summary['total_pages']}  "
          f"JS errors: {summary['total_js_errors']}  "
          f"failed requests: {summary['total_failed_requests']}  "
          f"problems: {classification['problem_count']}")
    print(f"[LIRIL-TEST] Handoff OK: {report['handoff'].get('autopilot_set_by_click')}")
    print(f"[LIRIL-TEST] Report: {REPORT_PATH}")
    print(f"[LIRIL-TEST] Public:  {PUBLIC_STATUS}")

    # Train LIRIL
    try:
        import subprocess
        topline = (
            f"liril_walkthrough_test status={status} "
            f"ok={summary['ok_pages']}/{summary['total_pages']} "
            f"js_errors={summary['total_js_errors']} "
            f"failed_requests={summary['total_failed_requests']} "
            f"problem_count={classification['problem_count']} "
            f"handoff_ok={handoff_result.get('autopilot_set_by_click')} "
            f"by_kind={json.dumps(classification['by_kind'])}"
        )
        subprocess.run(
            [r"E:\S.L.A.T.E\tenet5\.venv\Scripts\python.exe", str(TOOLS_ROOT / "liril_ask.py"),
             "train", topline, f"walkthrough_test_{status.lower()}"],
            check=False, cwd=r"E:\S.L.A.T.E\tenet5", capture_output=True, timeout=30,
        )
        print("[LIRIL-TEST] LIRIL trained on findings")
    except Exception as e:
        print(f"[LIRIL-TEST] LIRIL training failed: {e}")

    return 0 if status == "GREEN" else 1


if __name__ == "__main__":
    sys.exit(main())
