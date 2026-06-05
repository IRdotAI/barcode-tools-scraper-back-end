"""
Sainsbury's product search via headless Chrome.
Uses the approach that WORKED: navigate to search page, intercept API response.
"""

import asyncio
import json
import time
import logging
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Route

log = logging.getLogger("sainsburys")

_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_last_init: float = 0
_lock = asyncio.Lock()

SESSION_MAX_AGE = 3600


async def _ensure_browser():
    global _pw, _browser, _context, _last_init

    async with _lock:
        needs_init = (
            _browser is None
            or not _browser.is_connected()
            or (time.time() - _last_init) > SESSION_MAX_AGE
        )
        if not needs_init:
            return

        # Teardown old
        for r in (_context, _browser):
            if r:
                try:
                    await r.close()
                except Exception:
                    pass
        if _pw:
            try:
                await _pw.stop()
            except Exception:
                pass

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
        log.info("Chrome ready.")


async def search_sainsburys(query: str, page_size: int = 24) -> dict:
    await _ensure_browser()

    log.info(f"Searching: {query}")

    page = await _context.new_page()
    captured = {"result": None}
    api_event = asyncio.Event()

    async def intercept_api(route: Route):
        try:
            response = await route.fetch()
            body = await response.body()
            if response.status == 200 and body:
                try:
                    captured["result"] = json.loads(body)
                except json.JSONDecodeError:
                    pass
            await route.fulfill(response=response)
        except Exception as e:
            log.warning(f"Intercept error: {e}")
            try:
                await route.continue_()
            except Exception:
                pass
        api_event.set()

    await page.route("**/groceries-api/gol-services/product/v1/product*", intercept_api)

    try:
        search_url = f"https://www.sainsburys.co.uk/gol-ui/SearchResults/{query}"
        log.info(f"Navigating to: {search_url}")

        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)

        # Wait for API response
        try:
            await asyncio.wait_for(api_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            log.warning("API intercept timed out")

        if captured["result"] is None:
            title = await page.title()
            log.warning(f"No data captured. Title: {title}")
            raise Exception("No product data received")

    finally:
        await page.close()

    data = captured["result"]

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
