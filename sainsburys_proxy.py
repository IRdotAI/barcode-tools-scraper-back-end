import os
import time
import logging
from typing import Optional
from curl_cffi.requests import AsyncSession

log = logging.getLogger("sainsburys")

PROXY_HOST     = os.getenv("PROXY_HOST", "")
PROXY_PORT     = os.getenv("PROXY_PORT", "")
PROXY_USER     = os.getenv("PROXY_USER", "")
PROXY_PASS     = os.getenv("PROXY_PASS", "")
PROXY_PROTOCOL = os.getenv("PROXY_PROTOCOL", "http")
PROXY_COUNTRY  = os.getenv("PROXY_COUNTRY", "gb")


def _build_proxy_url() -> Optional[str]:
    if not PROXY_HOST or not PROXY_PORT:
        return None

    auth = ""
    if PROXY_USER and PROXY_PASS:
        auth = f"{PROXY_USER}:{PROXY_PASS}@"

    return f"{PROXY_PROTOCOL}://{auth}{PROXY_HOST}:{PROXY_PORT}"


PROXY_URL = _build_proxy_url()

SAINSBURYS_API = "https://www.sainsburys.co.uk/groceries-api/gol-services/product/v1/product"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.sainsburys.co.uk/gol-ui/SearchResults/milk",
    "Origin": "https://www.sainsburys.co.uk",
    "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_cache: dict = {}
CACHE_TTL = 1800


async def search_sainsburys(query: str, page_size: int = 24) -> dict:
    cache_key = f"{query.lower().strip()}:{page_size}"
    if cache_key in _cache:
        ts, cached_result = _cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            log.info(f"Cache hit: {query}")
            return cached_result

    if not PROXY_URL:
        raise Exception("Proxy not configured — set PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS env vars")

    log.info(f"Searching: {query} (via proxy)")

    params = {
        "filter[keyword]": query,
        "page_size": page_size,
        "page_number": 1,
        "sort_order": "RELEVANCE",
        "salesWindow": 1,
    }

    async with AsyncSession(impersonate="chrome") as session:
        response = await session.get(
            SAINSBURYS_API,
            params=params,
            headers=HEADERS,
            proxy=PROXY_URL,
            timeout=15,
        )

    if response.status_code == 403:
        log.warning(f"403 Forbidden — Akamai blocked (proxy: {PROXY_HOST})")
        raise Exception("Blocked by Akamai — try a different proxy region or check credentials")

    if response.status_code != 200:
        log.warning(f"API returned {response.status_code}")
        raise Exception(f"Sainsbury's API returned {response.status_code}")

    data = response.json()

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
    log.info(f"Found {total} products for: {query} ({response.elapsed:.1f}s)")

    result = {
        "query": query,
        "total_count": total,
        "products": products,
    }

    _cache[cache_key] = (time.time(), result)

    return result
