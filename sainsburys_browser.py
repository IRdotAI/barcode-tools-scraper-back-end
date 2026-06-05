"""
Sainsbury's product search via headless Chromium.

Instead of calling the API directly (which Akamai still blocks even with cookies),
we navigate to the actual search results page and intercept the API response
that Sainsbury's own JavaScript makes. The browser does everything naturally.
"""

import asyncio
import json
import time
import logging
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Route

log = logging.getLogger("sainsburys")

# ── Singleton browser manager ──────────────────────────────────────────────────

_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_last_init: float = 0
_lock = asyncio.Lock()

SESSION_MAX_AGE = 3600  # re-init session every 60 min


async def _boot():
    """Launch Chromium with stealth settings."""
    global _pw, _browser, _context, _page, _last_init

    log.info("Booting Playwright Chromium...")

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

    # Hide webdriver flag
    await _context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        // Spoof chrome runtime
        window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };
    """)

    _page = await _context.new_page()
    _last_init = time.time()
    log.info("Browser ready.")


async def _ensure_session():
    """Make sure we have a live browser, re-init if stale."""
    async with _lock:
        needs_init = (
            _browser is None
            or _page is None
            or _page.is_closed()
            or (time.time() - _last_init) > SESSION_MAX_AGE
        )
        if needs_init:
            await _teardown()
            await _boot()


async def _teardown():
    """Clean up browser resources."""
    global _pw, _browser, _context, _page
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


# ── Public API ─────────────────────────────────────────────────────────────────

async def search_sainsburys(query: str, page_size: int = 24) -> dict:
    """
    Search Sainsbury's by navigating to their search results page and
    intercepting the product API response their own JS makes.
    """
    await _ensure_session()

    log.info(f"Searching: {query}")

    # We'll capture the API response that Sainsbury's own JS makes
    captured_data = {"result": None, "error": None}
    api_event = asyncio.Event()

    async def intercept_api(route: Route):
        """Let the request through, but capture the response."""
        try:
            response = await route.fetch()
            body = await response.body()

            if response.status == 200 and body:
                try:
                    captured_data["result"] = json.loads(body)
                except json.JSONDecodeError:
                    pass

            # Forward the response to the page so it renders normally
            await route.fulfill(response=response)
        except Exception as e:
            captured_data["error"] = str(e)
            await route.continue_()

        api_event.set()

    # Intercept the product API call that the search page will make
    await _page.route("**/groceries-api/gol-services/product/v1/product*", intercept_api)

    try:
        # Navigate to the actual search results page
        search_url = f"https://www.sainsburys.co.uk/gol-ui/SearchResults/{query}"
        log.info(f"Navigating to: {search_url}")
        await _page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        # Wait a bit for JS to fire
        await _page.wait_for_timeout(3000)

        # Debug: log where we ended up
        final_url = _page.url
        title = await _page.title()
        log.info(f"Page loaded — URL: {final_url}, Title: {title}")

        # Wait for the API response to be captured (max 25 seconds)
        try:
            await asyncio.wait_for(api_event.wait(), timeout=25.0)
        except asyncio.TimeoutError:
            log.warning("API intercept timed out after 25s")

        # Debug: take screenshot for inspection
        try:
            await _page.screenshot(path="debug_search.png")
            log.info("Screenshot saved to debug_search.png")
        except Exception:
            pass

        # If we didn't capture the API response, try scraping the DOM instead
        if captured_data["result"] is None:
            log.warning(f"API intercept missed. Error: {captured_data.get('error')}")
            log.warning("Falling back to DOM scrape...")
            return await _scrape_dom(query)

        data = captured_data["result"]

    finally:
        # Remove the route handler so it doesn't stack up
        await _page.unroute("**/groceries-api/gol-services/product/v1/product*")

    # Normalise product data
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


async def _scrape_dom(query: str) -> dict:
    """
    Fallback: scrape product data directly from the rendered DOM
    if API interception didn't work.
    """
    log.info(f"DOM scrape fallback for: {query}")

    # Wait for product cards to appear
    try:
        await _page.wait_for_selector('[data-testid="product-tile"], .pt__content, .productLister-list li', timeout=15000)
    except Exception:
        # Maybe the page structure is different, try waiting for any content
        await _page.wait_for_timeout(5000)

    # Extract product data from the page
    products = await _page.evaluate("""
        () => {
            const items = [];

            // Try multiple possible selectors for product tiles
            const tiles = document.querySelectorAll(
                '[data-testid="product-tile"], .pt__content, .productLister-list li, [class*="product"]'
            );

            tiles.forEach(tile => {
                // Try to find product name
                const nameEl = tile.querySelector(
                    '[data-testid="product-tile--title"], .pt__title, h2, h3, [class*="title"]'
                );
                const name = nameEl ? nameEl.textContent.trim() : '';
                if (!name) return;

                // Try to find price
                const priceEl = tile.querySelector(
                    '[data-testid="product-tile--retail-price"], .pt__cost, [class*="price"]'
                );
                let price = null;
                if (priceEl) {
                    const priceText = priceEl.textContent.replace(/[^0-9.]/g, '');
                    if (priceText) price = parseFloat(priceText);
                }

                // Try to find image
                const imgEl = tile.querySelector('img[src*="sainsburys"], img[src*="product"]');
                const image = imgEl ? imgEl.src : '';

                // Try to find EAN/GTIN from data attributes or links
                let ean = '';
                const link = tile.querySelector('a[href*="/product/"]');
                if (link) {
                    // Sometimes the product ID is in the URL
                    const href = link.getAttribute('href') || '';
                    const match = href.match(/\\/product\\/[^/]+\\/(\\d{6,13})/);
                    if (match) ean = match[1];
                }

                items.push({ name, image, ean, price, brand: '', available: true, url: '' });
            });

            return items;
        }
    """)

    return {
        "query": query,
        "total_count": len(products),
        "products": products,
    }
