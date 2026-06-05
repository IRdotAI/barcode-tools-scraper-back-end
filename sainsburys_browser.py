"""
Sainsbury's product search via headless Chrome.

Startup: navigates to Sainsbury's to pass Akamai challenge (slow, once).
Searches: navigates same page to search URL (fast, cookies already set).
Intercepts the API response the instant it arrives.
"""

import asyncio
import json
import time
import logging
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

log = logging.getLogger("sainsburys")

_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_warmed = False
_last_init: float = 0
_lock = asyncio.Lock()

SESSION_MAX_AGE = 3600


async def _boot():
    global _pw, _browser, _context, _page, _warmed, _last_init

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

    # Warm up: navigate to Sainsbury's to pass Akamai challenge
    # This is slow (~30-40s) but only happens once
    log.info("Warming up — navigating to Sainsbury's...")
    await _page.goto(
        "https://www.sainsburys.co.uk/gol-ui/SearchResults/milk",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    await _page.wait_for_timeout(3000)

    title = await _page.title()
    log.info(f"Warm-up done — Title: {title}")

    _warmed = "Access Denied" not in title
    _last_init = time.time()

    if not _warmed:
        log.error("Akamai blocked warm-up navigation")


async def _ensure_browser():
    async with _lock:
        needs_init = (
            _browser is None
            or _page is None
            or _page.is_closed()
            or not _warmed
            or (time.time() - _last_init) > SESSION_MAX_AGE
        )
        if needs_init:
            await _teardown()
            await _boot()


async def _teardown():
    global _pw, _browser, _context, _page, _warmed
    _warmed = False
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
    _pw = _browser = _context = _page = None


async def search_sainsburys(query: str, page_size: int = 24) -> dict:
    """
    Navigate the warm page to a new search URL.
    Akamai cookies are already set from warm-up, so this should be fast.
    """
    await _ensure_browser()

    log.info(f"Searching: {query}")

    captured = {"data": None}
    api_event = asyncio.Event()

    async def on_response(response):
        url = response.url
        if "/groceries-api/gol-services/product/v1/product" in url:
            if response.status == 200:
                try:
                    body = await response.json()
                    captured["data"] = body
                except Exception:
                    pass
            else:
                log.warning(f"API returned {response.status}")
            api_event.set()

    _page.on("response", on_response)

    try:
        search_url = f"https://www.sainsburys.co.uk/gol-ui/SearchResults/{query}"
        await _page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for API response — should be fast since cookies are set
        try:
            await asyncio.wait_for(api_event.wait(), timeout=25.0)
        except asyncio.TimeoutError:
            log.warning("API response timeout")

    finally:
        _page.remove_listener("response", on_response)

    if captured["data"] is None:
        title = await _page.title()
        log.warning(f"No data. Title: {title}")
        raise Exception("No product data — try again")

    data = captured["data"]

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
    log.info(f"Found {total} products for: {query}")

    return {
        "query": query,
        "total_count": total,
        "products": products,
    }
