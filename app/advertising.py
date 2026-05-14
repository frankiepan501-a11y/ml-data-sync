"""ML advertising client — fetch monthly ad spend per campaign.

Discoveries (probed 2026-05-14):
  - GET /advertising/advertisers?product_id=PADS → advertiser_id per token namespace
  - GET /advertising/advertisers/{id}/product_ads/campaigns?date_from=...&date_to=...&metrics=cost,clicks,prints,acos
    Returns campaign list with embedded metrics dict per campaign.
  - cost is in advertiser's site currency (MLM=MXN, MLB=BRL; CBT advertiser site_id=MLM uses MXN too).

Cache: per (advertiser_id, month) 1-hour. The 5/min rate limit on /billing
doesn't seem to apply here — advertising endpoint has separate quota.

Phase B1 attribution: campaign names appear to be SKU-derived (e.g. "YS11-5 emgs",
"FF05-2 pro", "TZ06"). Use regex word-match to allocate cost to SKU; unmatched
cost flows to a synthetic `_unallocated_ads` bucket Feishu row.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from app import db

# Hard-coded advertiser_id mapping per child seller_id (discovered 2026-05-14).
# CBT 自发货 has no advertiser (doesn't run ads).
ADVERTISER_BY_SELLER: dict[int, int | None] = {
    1510203792: None,    # CBT 自发货
    1502236229: 501915,  # CBT-FULL — CNFUNLABMXF advertiser, site MLM
    1407362838: 38602,   # 本土 1 MX FUNLABDIRECTMX
    1436420028: 380587,  # 本土 2 MX FUNLAB_MX
    2378517428: 683851,  # 巴西 AIRSOFT (MLB)
}

# Auth-via: which user_id's token to use when calling /advertising for this seller.
# Same as token namespace; CBT sellers use CBT parent token.
TOKEN_USER_FOR_ADVERTISING: dict[int, int] = {
    1510203792: 1502520822,  # CBT parent
    1502236229: 1502520822,  # CBT parent
    1407362838: 1407362838,
    1436420028: 1436420028,
    2378517428: 2378517428,
}

# Currency advertiser bills in (per site_id).
AD_CURRENCY_BY_ADVERTISER: dict[int, str] = {
    501915: "MXN",  # CBT-FULL advertiser, billed in MXN
    38602: "MXN",
    380587: "MXN",
    683851: "BRL",
}

_campaign_cache: dict[tuple[int, str], dict] = {}  # (advertiser_id, month) → {results, cached_at}
_items_cache: dict[tuple[int, str], dict] = {}     # (advertiser_id, month) → {results, cached_at}


def _month_range(month: str) -> tuple[str, str]:
    """YYYY-MM → (YYYY-MM-01, YYYY-MM-DD-last). e.g. 2026-05 → (2026-05-01, 2026-05-31)."""
    import calendar
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{last:02d}"


async def fetch_campaigns_for_month(advertiser_id: int, month: str, token_user_id: int) -> list[dict]:
    """Return campaigns list with metrics for the given month. 1-hour cached."""
    key = (advertiser_id, month)
    cached = _campaign_cache.get(key)
    if cached and cached.get("_expires_at", 0) > time.time():
        return cached["results"]

    row = await db.get_token(token_user_id)
    if not row:
        return []
    headers = {"Authorization": f"Bearer {row['access_token']}"}

    date_from, date_to = _month_range(month)
    url = f"https://api.mercadolibre.com/advertising/advertisers/{advertiser_id}/product_ads/campaigns"
    params = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": "cost,clicks,prints,acos,direct_amount",
        "limit": 100,
        "offset": 0,
    }
    all_results: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                # Don't crash; just return empty (graceful degrade)
                break
            data = r.json()
            results = data.get("results") or []
            all_results.extend(results)
            paging = data.get("paging") or {}
            total = int(paging.get("total") or 0)
            if params["offset"] + len(results) >= total or not results:
                break
            params["offset"] += params["limit"]

    _campaign_cache[key] = {"results": all_results, "_expires_at": time.time() + 3600}
    return all_results


async def fetch_ad_items_for_month(advertiser_id: int, month: str, token_user_id: int) -> list[dict]:
    """Return ad items list with metrics for the given month. 1-hour cached.

    Endpoint: /advertising/advertisers/{id}/product_ads/items?date_from=...&date_to=...
    Each item has: item_id (ML listing ID), title, campaign_id, metrics.cost,
    metrics.clicks, metrics.prints.

    Preferred over fetch_campaigns_for_month for SKU attribution: ML listing
    item_id matches order_items[].item.id exactly, no name-regex needed.
    """
    key = (advertiser_id, month)
    cached = _items_cache.get(key)
    if cached and cached.get("_expires_at", 0) > time.time():
        return cached["results"]

    row = await db.get_token(token_user_id)
    if not row:
        return []
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    date_from, date_to = _month_range(month)
    url = f"https://api.mercadolibre.com/advertising/advertisers/{advertiser_id}/product_ads/items"
    params = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": "cost,clicks,prints",
        "limit": 50,
        "offset": 0,
    }
    all_results: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                break
            data = r.json()
            results = data.get("results") or []
            all_results.extend(results)
            paging = data.get("paging") or {}
            total = int(paging.get("total") or 0)
            if params["offset"] + len(results) >= total or not results:
                break
            params["offset"] += params["limit"]

    _items_cache[key] = {"results": all_results, "_expires_at": time.time() + 3600}
    return all_results


async def attribute_ad_cost_by_item_id(
    ad_items: list[dict],
    item_id_to_sku: dict[str, str],
    token_user_id: int | None = None,
) -> tuple[dict[str, float], float, list[str]]:
    """Attribute per-item ad cost to SKUs via item_id mapping.

    item_id_to_sku comes from already-cached orders (order_items[].item.id → seller_sku).
    For ad items whose item_id is NOT in the map (advertised but unsold this month),
    try cache_get_item then ML /items/{id} to fetch its seller_sku. If still no SKU
    found, the cost goes to `_unallocated_ads` bucket.

    Returns (sku_cost, unallocated_cost, advertised_unsold_skus).
    """
    sku_cost: dict[str, float] = {}
    unallocated = 0.0
    advertised_unsold: list[str] = []

    # Lazy fetch ml /items/{id} for unknown item_ids — share an httpx client + token
    fetch_token: str | None = None
    if token_user_id:
        trow = await db.get_token(token_user_id)
        if trow:
            fetch_token = trow["access_token"]

    async with httpx.AsyncClient(timeout=15) as client:
        for it in ad_items:
            cost = float((it.get("metrics") or {}).get("cost") or 0)
            if cost <= 0:
                continue
            item_id = it.get("item_id")
            if not item_id:
                unallocated += cost
                continue
            sku = item_id_to_sku.get(item_id)
            if not sku:
                # Try item cache first
                cached_item = await db.cache_get_item(item_id)
                if cached_item and cached_item.get("sku"):
                    sku = cached_item["sku"]
                elif fetch_token:
                    # Fallback: fetch from ML
                    try:
                        r = await client.get(
                            f"https://api.mercadolibre.com/items/{item_id}",
                            headers={"Authorization": f"Bearer {fetch_token}"},
                        )
                        if r.status_code == 200:
                            payload = r.json()
                            sku = payload.get("seller_custom_field") or ""
                            if not sku:
                                for a in (payload.get("attributes") or []):
                                    if a.get("id") == "SELLER_SKU":
                                        sku = a.get("value_name") or a.get("value_id") or ""
                                        break
                            if sku:
                                await db.cache_put_item(item_id, payload)
                    except Exception:
                        pass
            if sku:
                sku_cost[sku] = sku_cost.get(sku, 0.0) + cost
                if item_id not in item_id_to_sku:
                    advertised_unsold.append(sku)
            else:
                unallocated += cost

    return sku_cost, unallocated, advertised_unsold


def attribute_ad_cost_to_skus(campaigns: list[dict], known_skus: set[str]) -> tuple[dict[str, float], float]:
    """[DEPRECATED — use attribute_ad_cost_by_item_id] Attribute campaign cost via name regex match."""
    sku_cost: dict[str, float] = {}
    unallocated = 0.0
    for c in campaigns:
        cost = float((c.get("metrics") or {}).get("cost") or 0)
        if cost <= 0:
            continue
        name = c.get("name") or ""
        matched = [s for s in known_skus if re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", name)]
        if not matched:
            unallocated += cost
            continue
        per = cost / len(matched)
        for s in matched:
            sku_cost[s] = sku_cost.get(s, 0.0) + per
    return sku_cost, unallocated


# Site_id → estimated VAT rate (Phase B1: informational, not deducted from profit yet)
# MLM Mexico IVA 16%; MLB Brazil ICMS avg 18%; CBT-MX since ML withholds IVA upfront,
# seller's net already excludes it → 0 here.
VAT_RATE_BY_SITE: dict[str, float] = {
    "MLM": 0.16,
    "MLB": 0.18,
    "CBT": 0.0,  # ML withholds IVA at CBT level
}


def vat_for_site(site_id: str) -> float:
    return VAT_RATE_BY_SITE.get(site_id, 0.0)
