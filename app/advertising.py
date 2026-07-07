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

import os
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
    2378517428: 683851,   # 巴西 AIRSOFT (MLB)
    3383185411: 2909534,  # 本土 3 MX DISTRIBUIDOR VALMIGOZ
}

# Auth-via: which user_id's token to use when calling /advertising for this seller.
# Same as token namespace; CBT sellers use CBT parent token.
TOKEN_USER_FOR_ADVERTISING: dict[int, int] = {
    1510203792: 1502520822,  # CBT parent
    1502236229: 1502520822,  # CBT parent
    1407362838: 1407362838,
    1436420028: 1436420028,
    2378517428: 2378517428,
    3383185411: 3383185411,
}

# Visits API auth: which user_id's token to use for /users/{seller}/items_visits.
# CBT sellers are NOT accessible via CBT parent token (403 forbidden — ML treats
# child-seller visit data as private). Use the SAME seller_id's own token where
# available, else map to a token that has access.
VISITS_TOKEN_USER: dict[int, int | None] = {
    1510203792: None,  # CBT 自发货 — no token covers, skip
    1502236229: None,  # CBT-FULL — no token covers, skip
    1407362838: 1407362838,
    1436420028: 1436420028,
    2378517428: 2378517428,
    3383185411: 3383185411,
}

_visits_cache: dict[tuple[int, str], dict] = {}  # (seller_id, month) → {total, _expires_at}


async def fetch_shop_visits_for_month(seller_id: int, month: str) -> int | None:
    """Return total visits to this seller's listings for the given YYYY-MM. None if unavailable.

    Endpoint: GET /users/{seller_id}/items_visits?date_from=YYYY-MM-01&date_to=YYYY-MM-DD
    (date_to capped at today for current month — same convention as ads).

    CBT sellers return 403. We pre-populate VISITS_TOKEN_USER with None for them
    and skip the API call.
    """
    token_user = VISITS_TOKEN_USER.get(seller_id)
    if not token_user:
        return None
    key = (seller_id, month)
    cached = _visits_cache.get(key)
    if cached and cached.get("_expires_at", 0) > time.time():
        return cached.get("total")

    row = await db.get_token(token_user)
    if not row:
        return None
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    date_from, date_to = _month_range(month)
    url = f"https://api.mercadolibre.com/users/{seller_id}/items_visits"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, headers=headers, params={"date_from": date_from, "date_to": date_to})
            if r.status_code != 200:
                return None
            total = int((r.json() or {}).get("total_visits") or 0)
        except Exception:
            return None
    _visits_cache[key] = {"total": total, "_expires_at": time.time() + 3600}
    return total


# Site_id used in the marketplace advertising endpoint URL path.
# CBT-FULL advertiser registers under site=MLM (Mexico) since that's where it sells.
# CBT-FULL ads are also billed in USD (not MXN like local MX).
AD_SITE_BY_ADVERTISER: dict[int, str] = {
    501915: "MLM",  # CBT-FULL, billed in USD (CBT is USD-denominated)
    38602: "MLM",
    380587: "MLM",
    683851: "MLB",
    2909534: "MLM",
}

# Currency advertiser bills in.
AD_CURRENCY_BY_ADVERTISER: dict[int, str] = {
    501915: "USD",  # CBT-FULL — CBT advertiser bills in USD (verified 2026-05-14)
    38602: "MXN",
    380587: "MXN",
    683851: "BRL",
    2909534: "MXN",
}

_campaign_cache: dict[tuple[int, str], dict] = {}  # (advertiser_id, month) → {results, cached_at}
_items_cache: dict[tuple[int, str], dict] = {}     # (advertiser_id, month) → {results, cached_at}


def _month_range(month: str) -> tuple[str, str]:
    """YYYY-MM → (YYYY-MM-01, YYYY-MM-DD-last), capped at today.

    ML advertising endpoint silently returns 0 cost when date_to is in the future,
    so we clamp date_to to today for the current month.
    """
    import calendar
    from datetime import date
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    date_from = f"{y}-{m:02d}-01"
    today = date.today()
    if y == today.year and m == today.month:
        date_to = today.isoformat()
    else:
        date_to = f"{y}-{m:02d}-{last:02d}"
    return date_from, date_to


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


async def fetch_ad_items_for_month(advertiser_id: int, month: str, token_user_id: int, *, strict: bool = False) -> list[dict]:
    """Return ad items list with metrics for the given month. 1-hour cached.

    Endpoint (Marketplace Advertising API v2):
      GET /marketplace/advertising/{SITE}/advertisers/{id}/product_ads/ads/search
      Headers: api-version: 2
      Params: date_from, date_to, metrics, metrics_summary=true, limit, offset

    Each result has item_id (ML listing ID, matches order_items[].item.id), title,
    campaign_id, metrics.{cost,clicks,prints}.

    Works uniformly for CBT advertisers (site=MLM, currency=USD) and local
    advertisers (site=MLM/MLB, currency=MXN/BRL). Replaces deprecated
    /advertising/advertisers/{id}/product_ads/items endpoint.
    """
    key = (advertiser_id, month)
    cached = _items_cache.get(key)
    if cached and cached.get("_expires_at", 0) > time.time():
        return cached["results"]

    site = AD_SITE_BY_ADVERTISER.get(advertiser_id)
    if not site:
        if strict:
            raise RuntimeError(f"advertising site is not configured for advertiser_id={advertiser_id}")
        return []

    row = await db.get_token(token_user_id)
    if not row:
        if strict:
            raise RuntimeError(f"advertising token not found for token_user_id={token_user_id}")
        return []
    headers = {
        "Authorization": f"Bearer {row['access_token']}",
        "api-version": "2",
    }
    date_from, date_to = _month_range(month)
    url = f"https://api.mercadolibre.com/marketplace/advertising/{site}/advertisers/{advertiser_id}/product_ads/ads/search"
    # Valid ML metrics (probed 2026-05-14): cost / clicks / prints / cpc / roas / acos
    # / direct_amount / indirect_amount / total_amount.
    # Invalid (do NOT include): units, orders, conversions, direct_units.
    params: dict[str, Any] = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": "cost,clicks,prints,direct_amount,indirect_amount,total_amount,direct_items_quantity,indirect_items_quantity",
        "metrics_summary": "true",
        "limit": 50,
        "offset": 0,
    }
    all_results: list[dict] = []
    had_error = False
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                had_error = True
                if strict:
                    raise RuntimeError(f"ML ads API failed advertiser_id={advertiser_id} status={r.status_code}: {r.text[:300]}")
                break
            data = r.json()
            results = data.get("results") or []
            all_results.extend(results)
            paging = data.get("paging") or {}
            total = int(paging.get("total") or 0)
            if params["offset"] + len(results) >= total or not results:
                break
            params["offset"] += params["limit"]

    # Only cache successful non-empty results. If ML rate-limited / 5xx,
    # next call should retry rather than serve stale empty data.
    if not had_error and all_results:
        _items_cache[key] = {"results": all_results, "_expires_at": time.time() + 3600}
    return all_results


async def attribute_ad_metrics_by_item_id(
    ad_items: list[dict],
    item_id_to_sku: dict[str, str],
    token_user_id: int | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, float], list[str]]:
    """Attribute per-item ad METRICS (cost, clicks, prints, direct/indirect/total_amount)
    to SKUs via item_id mapping.

    Returns:
      sku_metrics: {sku: {cost, clicks, prints, direct_amount, indirect_amount, total_amount}}
      unallocated_metrics: same shape for items with no SKU resolution
      advertised_unsold: SKUs whose item_id was advertised but did not appear in cached orders
    """
    sku_metrics: dict[str, dict[str, float]] = {}
    unallocated_metrics: dict[str, float] = {
        "cost": 0.0, "clicks": 0.0, "prints": 0.0,
        "direct_amount": 0.0, "indirect_amount": 0.0, "total_amount": 0.0,
        "direct_items_quantity": 0.0, "indirect_items_quantity": 0.0,
    }
    advertised_unsold: list[str] = []

    fetch_token: str | None = None
    if token_user_id:
        trow = await db.get_token(token_user_id)
        if trow:
            fetch_token = trow["access_token"]

    def _add(target: dict[str, float], m: dict):
        for k in ("cost", "clicks", "prints",
                 "direct_amount", "indirect_amount", "total_amount",
                 "direct_items_quantity", "indirect_items_quantity"):
            target[k] = target.get(k, 0.0) + float(m.get(k) or 0)

    async with httpx.AsyncClient(timeout=15) as client:
        for it in ad_items:
            m = it.get("metrics") or {}
            # Skip if no signal at all
            if not any((m.get("cost"), m.get("clicks"), m.get("prints"), m.get("total_amount"))):
                continue
            item_id = it.get("item_id")
            if not item_id:
                _add(unallocated_metrics, m)
                continue
            sku = item_id_to_sku.get(item_id)
            if not sku:
                cached_item = await db.cache_get_item(item_id)
                if cached_item and cached_item.get("sku"):
                    sku = cached_item["sku"]
                elif fetch_token:
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
                cell = sku_metrics.setdefault(sku, {})
                _add(cell, m)
                if item_id not in item_id_to_sku:
                    advertised_unsold.append(sku)
            else:
                _add(unallocated_metrics, m)

    return sku_metrics, unallocated_metrics, advertised_unsold


# Backward compat wrapper (still used by some flows internally; returns only cost dict)
async def attribute_ad_cost_by_item_id(
    ad_items: list[dict],
    item_id_to_sku: dict[str, str],
    token_user_id: int | None = None,
) -> tuple[dict[str, float], float, list[str]]:
    sku_m, unalloc_m, advert_unsold = await attribute_ad_metrics_by_item_id(
        ad_items, item_id_to_sku, token_user_id
    )
    sku_cost = {k: v.get("cost", 0.0) for k, v in sku_m.items()}
    return sku_cost, unalloc_m.get("cost", 0.0), advert_unsold


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


# Site_id → 实际预扣税率 (deducted from profit). 2026-06-17 校准:
#   🚨 MLM 本土店不是 16% IVA! 官方"Ventas"导出 I列(Cargo por venta e impuestos)=佣金+税合并;
#      实测逐单 I/H 恒=25.05% = 佣金16%(API sale_fee, 随listing_type变) + 税9.05%(法定预扣).
#      税 = ML 代墨西哥 SAT 预扣 IVA retención(~8%) + ISR retención(~1%), 按收入恒定%, 非 16% IVA.
#      税**只在订单报表 I列**, order API(taxes.amount=null/taxes_amount=0) 和 billing API(账单只有
#      佣金/运费/广告/仓储4类, 无 IVA 行) **都不暴露** → A 引擎只能用此校准率, B 用导出 I列−佣金 做真值,
#      每月 A vs B 对账校准. 实测 13687.45 MXN 收入 → 税 1238.89(9.0512%). env MLM_TAX_RATE 可调.
#   🚨 MLB 巴西不是 18% ICMS! (旧假设错). 官方"Vendas BR"导出 K列(Tarifa de venda e impostos)=佣金+税合并;
#      sale_fee 实测=per-unit(order qty=2/unit_price=49.9/sale_fee=6.49=单单位)要×qty(同墨西哥原生,非CBT per-line).
#      🚨 税率必须**订单级JOIN同一订单集**算, 不能用聚合差(不同订单集的聚合差含订单集错配, 非纯税!Frankie 2026-06-18 纠正).
#      JOIN venta=pack_id 匹配452单(A营收=B营收=28349.55 完全一致): A佣金(sale_fee×qty)=3597.94 vs B K列=3766.13
#      → 真税=168.19 BRL=营收 0.593%. **逐单89%税=0, 集中11%订单**(疑特定高客单SKU巴西税); flat 0.593%是其聚合近似.
#      巴西 ML 税(impostos)非销售 ICMS(营收H是卖家净额). B 用导出 K列-API佣金(订单级匹配)做真值月度对账. env BR_TAX_RATE 可调.
#   CBT-MX 走 cbt-pnl-api 单算(13.8%), 主sync不重复扣→0.
VAT_RATE_BY_SITE: dict[str, float] = {
    "MLM": float(os.getenv("MLM_TAX_RATE", "0.0905")),
    "MLB": float(os.getenv("BR_TAX_RATE", "0.0059")),
    # CBT-MX 税(IVA 代扣)按 K×13.8% 扣(2026-06-17 收口校验: 官方 Orders report Q税列实测 13.82% ✓).
    # 旧注释"seller's net already excludes it→0"是错的: 导出 Q税列是独立扣项, 从 S净受领中扣减.
    # 与 main.py CBT_TAX_RATE 同 env 同默认, 保持主sync 与 cbt-pnl-api 口径一致.
    "CBT": float(os.getenv("CBT_TAX_RATE", "0.138")),
}


def vat_for_site(site_id: str) -> float:
    return VAT_RATE_BY_SITE.get(site_id, 0.0)
