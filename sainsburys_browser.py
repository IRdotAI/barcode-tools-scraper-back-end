"""
Sainsbury's product search via headless Chrome.

1. First request: navigate to sainsburys.co.uk to complete Akamai JS challenge
2. All subsequent searches: call the API directly from inside the browser
   (same cookies, same TLS fingerprint, no page navigation needed = fast)
"""

import asyncio
import time
import logging
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

log = logging.getLogger("sainsburys")

_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_last_init: float = 0
_session_ready = False
_lock = asyncio.Lock()

SESSION_MAX_AGE = 3600


async def _boot():
    """Launch Chrome and navigate to Sainsbury's to seed Akamai cookies."""
    global _pw, _browser, _context, _page, _last_init, _session_ready

    log.info("Booting Playwright Chrome...")

    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        channel="chrome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    _context = await _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="en-GB",
        timezone_id="Europe/London",
    )

    await _context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };
    """)

    _page = await _context.new_page()

    # Navigate to Sainsbury's to trigger Akamai challenge and set cookies
    log.info("Navigating to Sainsbury's homepage...")
    await _page.goto(
        "https://www.sainsburys.co.uk/gol-ui/SearchResults/milk",
        wait_until="networkidle",
        timeout=45000,
    )

    title = await _page.title()
    log.info(f"Page loaded — Title: {title}")

    if "Access Denied" in title:
        raise Exception("Akamai blocked the session")

    # Give cookies a moment to settle
    await _page.wait_for_timeout(2000)

    _last_init = time.time()
    _session_ready = True
    log.info("Session ready — subsequent searches will use fast API calls.")


async def _ensure_session():
    async with _lock:
        needs_init = (
            _browser is None
            or _page is None
            or _page.is_closed()
            or not _session_ready
            or (time.time() - _last_init) > SESSION_MAX_AGE
        )
        if needs_init:
            await _teardown()
            await _boot()


async def _teardown():
    global _pw, _browser, _context, _page, _session_ready
    _session_ready = False
    for resource in (_page, _context, _browser):
        if resource:
            try:
                await resource.close()
            except Exception:
                pass
    if _pw:
        try:
            await _pw.stop()
        except Exception:
            pass
    _page = _context = _browser = _pw = None


async def search_sainsburys(query: str, page_size: int = 24) -> dict:
    """Search Sainsbury's — fast API call from inside the browser context."""
    await _ensure_session()

    log.info(f"Searching: {query}")

    # Call the Sainsbury's API directly from inside the browser
    # Same origin, same cookies, same TLS — Akamai sees a normal browser
    result = await _page.evaluate("""
        async ({ query, pageSize }) => {
            const url = '/groceries-api/gol-services/product/v1/product' +
                '?filter[keyword]=' + encodeURIComponent(query) +
                '&page_size=' + pageSize +
                '&page_number=1' +
                '&sort_order=RELEVANCE' +
                '&salesWindow=1';

            try {
                const res = await fetch(url, {
                    headers: { 'Accept': 'application/json' }
                });

                if (!res.ok) {
                    return { error: 'HTTP ' + res.status, status: res.status };
                }

                const data = await res.json();
                return { ok: true, data: data };
            } catch (err) {
                return { error: err.message || 'fetch failed' };
            }
        }
    """, {"query": query, "pageSize": page_size})

    # If 403, session expired — refresh and retry once
    if result.get("status") == 403:
        log.warning("Got 403 — refreshing session...")
        async with _lock:
            await _teardown()
            await _boot()

        result = await _page.evaluate("""
            async ({ query, pageSize }) => {
                const url = '/groceries-api/gol-services/product/v1/product' +
                    '?filter[keyword]=' + encodeURIComponent(query) +
                    '&page_size=' + pageSize +
                    '&page_number=1&sort_order=RELEVANCE&salesWindow=1';
                try {
                    const res = await fetch(url, {
                        headers: { 'Accept': 'application/json' }
                    });
                    if (!res.ok) return { error: 'HTTP ' + res.status, status: res.status };
                    const data = await res.json();
                    return { ok: true, data: data };
                } catch (err) {
                    return { error: err.message || 'fetch failed' };
                }
            }
        """, {"query": query, "pageSize": page_size})

    if not result.get("ok"):
        raise Exception(f"Sainsbury's API: {result.get('error', 'unknown')}")

    data = result["data"]

    products = []
    for p in data.get("products", []):
        ean = ""
        if p.get("eans"):
            ean = p["eans"][0]
        elif p.get("gtin"):
            ean = p["gtin"]

        products.append({
            "name":      p.get("name", ""),
            "image":     p.get("image_thumbnail") or p.get("image", ""),
            "ean":       ean,
            "price":     (p.get("retail_price") or {}).get("price"),
            "brand":     ((p.get("attributes") or {}).get("brand") or [""])[0],
            "available": p.get("is_available", True),
            "url":       p.get("full_url", ""),
        })

    total = (data.get("controls") or {}).get("total_record_count", len(products))

    return {
        "query": query,
        "total_count": total,
        "products": products,
    }
