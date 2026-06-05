"""
Sainsbury's product search via headless Chrome.

Navigates to the actual search results page and intercepts the API
response that Sainsbury's own JS makes. Returns as soon as the API
response is captured — doesn't wait for images/CSS/full render.
"""

import asyncio
import json
import time
import logging
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Route

log = logging.getLogger("sainsburys")

_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_last_init: float = 0
_lock = asyncio.Lock()

SESSION_MAX_AGE = 3600


async def _boot():
    global _pw, _browser, _context, _last_init

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

    _last_init = time.time()
    log.info("Browser ready.")


async def _ensure_browser():
    async with _lock:
        needs_init = (
            _browser is None
            or not _browser.is_connected()
            or (time.time() - _last_init) > SESSION_MAX_AGE
        )
        if needs_init:
            await _teardown()
            await _boot()


async def _teardown():
    global _pw, _browser, _context
    for resource in (_context, _browser):
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
    _pw = _browser = _context = None


async def search_sainsburys(query: str, page_size: int = 24) -> dict:
    """
    Navigate to Sainsbury's search page in a fresh tab, intercept
    the product API response, close the tab. Fast because we don't
    wait for full page render — just the API JSON.
    """
    await _ensure_browser()

    log.info(f"Searching: {query}")

    # Fresh page per search (avoids stale state, closes cleanly)
    page = await _context.new_page()
    captured = {"data": None}
    api_event = asyncio.Event()

    async def on_response(response):
        """Capture the product API response as soon as it arrives."""
        url = response.url
        if "/groceries-api/gol-services/product/v1/product" in url and response.status == 200:
            try:
                body = await response.json()
                captured["data"] = body
                api_event.set()
            except Exception:
                pass

    page.on("response", on_response)

    try:
        search_url = f"https://www.sainsburys.co.uk/gol-ui/SearchResults/{query}"

        # Navigate — don't wait for full load, just until DOM is ready
        await page.goto(search_url, wait_until="commit", timeout=30000)

        # Wait for the API response (not the full page render)
        try:
            await asyncio.wait_for(api_event.wait(), timeout=25.0)
        except asyncio.TimeoutError:
            log.warning(f"API response timeout for: {query}")

        if captured["data"] is None:
            # Check page title — might be Access Denied
            title = await page.title()
            log.warning(f"No API data captured. Page title: {title}")
            raise Exception("No product data received — Sainsbury's may be blocking this server")

        data = captured["data"]

    finally:
        await page.close()

    # Normalise
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
