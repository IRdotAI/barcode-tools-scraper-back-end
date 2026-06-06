import os
import logging
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from scraper import scrape_wethrift_coupons

logging.basicConfig(level=logging.INFO)

# Use proxy-based search (fast) if proxy is configured, else fall back to Playwright
PROXY_HOST = os.getenv("PROXY_HOST", "")

if PROXY_HOST:
    logging.info("Proxy configured — using direct API mode (fast)")
    from sainsburys_proxy import search_sainsburys
else:
    logging.info("No proxy configured — falling back to Playwright Chrome mode")
    from sainsburys_browser import search_sainsburys


app = FastAPI(title="Barcode Tools API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    mode = "proxy" if PROXY_HOST else "playwright"
    return {"status": "ok", "mode": mode, "message": "Barcode Tools API is running."}


@app.get("/api/scrape")
def scrape_coupons(
    store: str = Query(..., description="Store name"),
    min_discount: str = Query("any", description="Minimum discount filter"),
):
    try:
        coupons = scrape_wethrift_coupons(store)
        if min_discount != "any" and min_discount.isdigit():
            import re
            min_val = int(min_discount)
            coupons = [
                c for c in coupons
                if any(int(n) >= min_val for n in re.findall(r'\d+', c['discount']))
            ]
        return {"store": store, "count": len(coupons), "coupons": coupons}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sainsburys")
async def sainsburys_search(
    q: str = Query(..., description="Product search term"),
    page_size: int = Query(24, description="Results per page"),
):
    try:
        result = await search_sainsburys(q, page_size)
        return result
    except Exception as e:
        logging.error(f"Sainsbury's search failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
