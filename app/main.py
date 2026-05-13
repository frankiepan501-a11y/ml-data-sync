"""ML Direct Sync — FastAPI entrypoint.

M2: tokens persisted to SQLite; admin endpoints for seed/list/refresh.
"""

import os
import secrets
import httpx
from fastapi import FastAPI, HTTPException, Request, Depends, Header
from dotenv import load_dotenv

from app import db

load_dotenv()

app = FastAPI(title="ML Direct Sync", version="0.3.0")


# ---------- bootstrap ----------

@app.on_event("startup")
async def _startup() -> None:
    await db.init_db()


# ---------- auth dep ----------

def require_service_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("SERVICE_AUTH_TOKEN")
    if not expected:
        raise HTTPException(500, "SERVICE_AUTH_TOKEN not configured")
    got = (authorization or "").removeprefix("Bearer ").strip()
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(401, "invalid service token")


# ---------- public ----------

@app.get("/")
def root():
    return {"service": "ml-data-sync", "version": "0.3.0", "milestone": "M2"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/oauth/authorize-url")
def authorize_url(site: str = "CBT"):
    """Build the ML OAuth authorization URL.

    For CBT (Global Selling) accounts, use `site=CBT` (default) — the auth
    domain MUST be `global-selling.mercadolibre.com` (no country suffix),
    not `auth.mercadolibre.com.??`.
    """
    site = site.upper()
    if site == "CBT":
        host = "global-selling.mercadolibre.com"
    else:
        tld_map = {
            "MLM": "com.mx",
            "MLA": "com.ar",
            "MLB": "com.br",
            "MLC": "cl",
            "MCO": "com.co",
        }
        host = f"auth.mercadolibre.{tld_map.get(site, 'com.mx')}"

    app_id = os.getenv("ML_APP_ID", "")
    redirect = os.getenv("ML_REDIRECT_URI", "")
    if not app_id or not redirect:
        raise HTTPException(500, "ML_APP_ID or ML_REDIRECT_URI not configured")
    import urllib.parse
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": redirect,
        "scope": "offline_access read",
    })
    return {"site": site, "host": host, "url": f"https://{host}/authorization?{qs}"}


# ---------- OAuth callback ----------

async def _exchange_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.mercadolibre.com/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": os.getenv("ML_APP_ID"),
                "client_secret": os.getenv("ML_APP_SECRET"),
                "code": code,
                "redirect_uri": os.getenv("ML_REDIRECT_URI"),
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"ML token exchange failed: {resp.text}")
    return resp.json()


async def _fetch_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.mercadolibre.com/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    return resp.json() if resp.status_code == 200 else {}


@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return {"status": "error", "error": error}
    if not code:
        raise HTTPException(400, "missing code parameter")

    data = await _exchange_code(code)
    user_id = data.get("user_id")
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in") or 21600
    scope = data.get("scope")

    user_info = await _fetch_user_info(access_token) if access_token else {}
    nickname = user_info.get("nickname")
    site_id = user_info.get("site_id")

    await db.upsert_token(
        user_id=user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scope=scope,
        nickname=nickname,
        site_id=site_id,
    )
    print(f"[OAUTH STORED] user_id={user_id} nickname={nickname} site={site_id} has_refresh={bool(refresh_token)}", flush=True)

    return {
        "status": "success",
        "user_id": user_id,
        "nickname": nickname,
        "site_id": site_id,
        "scope_has_offline_access": "offline_access" in (scope or ""),
        "has_refresh_token": bool(refresh_token),
        "expires_in": expires_in,
        "note": "Token persisted server-side. You can close this page.",
    }


# ---------- admin (Bearer) ----------

@app.get("/admin/tokens", dependencies=[Depends(require_service_token)])
async def admin_list_tokens():
    rows = await db.list_tokens()
    return {"count": len(rows), "tokens": [db.redact(r) for r in rows]}


@app.post("/admin/seed", dependencies=[Depends(require_service_token)])
async def admin_seed(req: Request):
    """One-time seed: insert/update a token row from external source.

    Body: { "user_id": ..., "access_token": "...", "refresh_token": "...",
            "expires_in": 21600, "scope": "...", "nickname": "...", "site_id": "..." }
    """
    body = await req.json()
    required = ("user_id", "access_token", "expires_in")
    missing = [k for k in required if k not in body]
    if missing:
        raise HTTPException(400, f"missing fields: {missing}")
    await db.upsert_token(
        user_id=int(body["user_id"]),
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=int(body["expires_in"]),
        scope=body.get("scope"),
        nickname=body.get("nickname"),
        site_id=body.get("site_id"),
    )
    row = await db.get_token(int(body["user_id"]))
    return {"status": "seeded", "token": db.redact(row) if row else None}


async def _refresh_one(user_id: int) -> dict:
    row = await db.get_token(user_id)
    if not row:
        raise HTTPException(404, f"no token for user_id={user_id}")
    if not row.get("refresh_token"):
        raise HTTPException(400, f"user_id={user_id} has no refresh_token (re-authorize required)")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.mercadolibre.com/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": os.getenv("ML_APP_ID"),
                "client_secret": os.getenv("ML_APP_SECRET"),
                "refresh_token": row["refresh_token"],
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"refresh failed: {resp.text}")
    data = resp.json()
    await db.upsert_token(
        user_id=int(data["user_id"]),
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=int(data.get("expires_in") or 21600),
        scope=data.get("scope"),
        nickname=row.get("nickname"),
        site_id=row.get("site_id"),
    )
    new_row = await db.get_token(int(data["user_id"]))
    return db.redact(new_row) if new_row else {}


@app.post("/admin/refresh/{user_id}", dependencies=[Depends(require_service_token)])
async def admin_refresh_one(user_id: int):
    return {"status": "refreshed", "token": await _refresh_one(user_id)}


@app.post("/admin/refresh-expiring", dependencies=[Depends(require_service_token)])
async def admin_refresh_expiring(within_seconds: int = 1800):
    """Refresh all tokens expiring within `within_seconds` (default 30 min).

    Suitable target for an n8n cron job running every ~25 minutes.
    """
    rows = await db.list_expiring(within_seconds)
    results = []
    for row in rows:
        try:
            results.append({"user_id": row["user_id"], "ok": True, "token": await _refresh_one(row["user_id"])})
        except HTTPException as e:
            results.append({"user_id": row["user_id"], "ok": False, "error": str(e.detail)})
    return {"checked": len(rows), "results": results}


# ---------- M3 reporting ----------

@app.get("/report/debug-order-detail", dependencies=[Depends(require_service_token)])
async def debug_order_detail(order_id: int, parent_user_id: int = 1502520822):
    """Fetch a single order's detailed schema via /marketplace/orders/{id} and /orders/{id}."""
    row = await db.get_token(parent_user_id)
    if not row:
        raise HTTPException(404, "token not found")
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    async with httpx.AsyncClient(timeout=30) as client:
        out = {}
        for ep in [
            f"https://api.mercadolibre.com/marketplace/orders/{order_id}",
            f"https://api.mercadolibre.com/orders/{order_id}",
        ]:
            r = await _ml_get(client, ep, headers)
            out[ep] = {"status": r.status_code, "body": (r.json() if r.status_code == 200 else r.text[:200])}
        return out


@app.get("/report/debug-orders-first-page", dependencies=[Depends(require_service_token)])
async def debug_orders_first_page(
    seller_id: int,
    month: str,
    parent_user_id: int = 1502520822,
    limit: int = 5,
):
    """Pull just the first N orders for a seller in a month — to validate date filter actually works."""
    row = await db.get_token(parent_user_id)
    if not row:
        raise HTTPException(404, "token not found")
    yyyy, mm = [int(x) for x in month.split("-")]
    date_from = f"{yyyy}-{mm:02d}-01T00:00:00.000-00:00"
    dty, dtm = (yyyy + 1, 1) if mm == 12 else (yyyy, mm + 1)
    date_to = f"{dty}-{dtm:02d}-01T00:00:00.000-00:00"

    headers = {"Authorization": f"Bearer {row['access_token']}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await _ml_get(
            client,
            "https://api.mercadolibre.com/marketplace/orders/search",
            headers,
            {
                "seller": seller_id,
                "order.date_created.from": date_from,
                "order.date_created.to": date_to,
                "limit": limit,
                "sort": "date_desc",
            },
        )
    return {
        "request_params": {"seller": seller_id, "date_from": date_from, "date_to": date_to, "limit": limit},
        "status": r.status_code,
        "ml_response": r.json() if r.status_code == 200 else r.text[:1000],
    }


import asyncio as _asyncio
import random as _random
from aiolimiter import AsyncLimiter

# Token buckets per ML endpoint class.
# ML real limits (researched 2026-05-13):
#   /orders/search, /items/search  → 100 req/min  (hard wall, easy to hit)
#   /items/{id}, /orders/{id}, /users/me, etc  → 1500 req/min
#   /oauth/token refresh           → very strict (~2/hour); leave manual
# Leave 20% headroom on each.
_LIMIT_SEARCH = AsyncLimiter(80, 60)    # 80 / 60s
_LIMIT_DETAIL = AsyncLimiter(1200, 60)  # 1200 / 60s


def _pick_bucket(url: str) -> AsyncLimiter:
    if "/search" in url:
        return _LIMIT_SEARCH
    return _LIMIT_DETAIL


async def _ml_get(client: httpx.AsyncClient, url: str, headers: dict, params: dict | None = None, max_retries: int = 4) -> httpx.Response:
    """httpx GET with token-bucket rate limiting + Retry-After-aware exponential backoff."""
    bucket = _pick_bucket(url)
    last = None
    for attempt in range(max_retries):
        async with bucket:
            r = await client.get(url, headers=headers, params=params)
        last = r
        if r.status_code != 429:
            return r
        retry_after = int(r.headers.get("Retry-After", "0") or "0")
        # Retry-After honored if present, else exponential 2/4/8/16 + jitter, capped at 60
        wait = retry_after if retry_after > 0 else min(60, (2 ** (attempt + 1)) + _random.uniform(0, 5))
        await _asyncio.sleep(wait)
    return last  # type: ignore[return-value]


async def _fetch_seller_items_with_sku(client: httpx.AsyncClient, headers: dict, seller_id: int) -> dict[str, dict]:
    """Return {item_id: {sku, title, price, currency, status}} for all of a seller's listings."""
    items: dict[str, dict] = {}
    offset = 0
    while True:
        r = await _ml_get(
            client,
            f"https://api.mercadolibre.com/marketplace/users/{seller_id}/items/search",
            headers,
            {"limit": 50, "offset": offset},
        )
        if r.status_code != 200:
            raise HTTPException(502, f"items/search failed seller={seller_id} offset={offset} status={r.status_code} body={r.text[:300]}")
        try:
            data = r.json()
        except Exception:
            raise HTTPException(502, f"items/search non-JSON seller={seller_id} status={r.status_code} ct={r.headers.get('content-type')} body={r.text[:300]}")
        ids = data.get("results", [])
        if not ids:
            break
        for item_id in ids:
            r2 = await _ml_get(client, f"https://api.mercadolibre.com/items/{item_id}", headers)
            if r2.status_code != 200:
                items[item_id] = {"sku": "(item_403)", "title": None, "price": None}
                continue
            it = r2.json()
            sku = it.get("seller_custom_field")
            if not sku:
                for a in (it.get("attributes") or []):
                    if a.get("id") == "SELLER_SKU":
                        sku = a.get("value_name") or a.get("value_id")
                        break
            items[item_id] = {
                "sku": sku or "(no_sku)",
                "title": it.get("title"),
                "price": it.get("price"),
                "currency_id": it.get("currency_id"),
                "status": it.get("status"),
            }
        if len(ids) < 50:
            break
        offset += 50
    return items


@app.get("/report/sku-recent", dependencies=[Depends(require_service_token)])
async def report_sku_recent(
    seller_id: int,
    recent_n: int = 100,
    parent_user_id: int = 1502520822,
):
    """SKU aggregation over the most recent N orders (fast PoC).

    Strategy:
      1. /marketplace/orders/search?seller=X&limit=N&sort=date_asc — pull recent pack list
      2. For each pack: take inner orders[].id, fetch /marketplace/orders/{id} for date + amount + SKU
      3. Aggregate by SKU: count + sum(paid_amount) + min/max date_created

    Trade-off vs strict month filter: marketplace search doesn't accept date filter,
    so true monthly aggregation requires full pagination (slow). M3.1 will fix this.
    """
    import traceback
    try:
        return await _report_sku_recent_impl(seller_id, recent_n, parent_user_id)
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()}


async def _report_sku_recent_impl(seller_id: int, recent_n: int, parent_user_id: int):
    row = await db.get_token(parent_user_id)
    if not row:
        raise HTTPException(404, "parent token not found in DB; seed first")
    headers = {"Authorization": f"Bearer {row['access_token']}"}

    async with httpx.AsyncClient(timeout=120) as client:
        # 1. pull pack list (last N) — orders/search is critical; allow ONE 30s retry on 429
        packs: list[dict] = []
        offset = 0
        while len(packs) < recent_n:
            page_size = min(50, recent_n - len(packs))
            params = {"seller": seller_id, "limit": page_size, "offset": offset, "sort": "date_desc"}
            r = await _ml_get(client, "https://api.mercadolibre.com/marketplace/orders/search", headers, params)
            if r.status_code == 429:
                await _asyncio.sleep(30)
                r = await _ml_get(client, "https://api.mercadolibre.com/marketplace/orders/search", headers, params)
            if r.status_code != 200:
                raise HTTPException(502, f"orders/search failed after retry status={r.status_code} body={r.text[:300]}")
            rr = r.json().get("results", [])
            if not rr:
                break
            packs.extend(rr)
            if len(rr) < page_size:
                break
            offset += page_size

        # 2. fetch detail for each inner order — with SQLite cache (Phase 1·④)
        order_details: list[dict] = []
        skipped_429 = 0
        skipped_other = 0
        cache_hits = 0
        for pack in packs:
            for sub in (pack.get("orders") or []):
                order_id = sub.get("id")
                if not order_id:
                    continue
                # Cache check first — order details are immutable once paid
                cached = await db.cache_get_order(int(order_id))
                if cached and cached.get("_payload"):
                    order_details.append(cached["_payload"])
                    cache_hits += 1
                    continue
                rd = await _ml_get(client, f"https://api.mercadolibre.com/marketplace/orders/{order_id}", headers)
                if rd.status_code == 200:
                    detail = rd.json()
                    order_details.append(detail)
                    await db.cache_put_order(int(order_id), seller_id, detail)
                elif rd.status_code == 429:
                    skipped_429 += 1
                else:
                    skipped_other += 1

    # 3. aggregate by seller_sku
    by_sku: dict[str, dict] = {}
    for od in order_details:
        for item in (od.get("order_items") or []):
            it = item.get("item") or {}
            sku = it.get("seller_sku") or it.get("seller_custom_field") or "(no_sku)"
            currency = (it.get("global_price") or {}).get("currency") or "?"
            amount = float((it.get("global_price") or {}).get("amount") or 0)
            quantity = int(item.get("quantity") or 1)
            cell = by_sku.setdefault(sku, {
                "sku": sku,
                "orders_count": 0,
                "units": 0,
                "revenue_total": 0.0,
                "currency": currency,
                "sample_title": it.get("title"),
                "sample_item_id": it.get("id"),
                "first_seen": None,
                "last_seen": None,
            })
            cell["orders_count"] += 1
            cell["units"] += quantity
            cell["revenue_total"] += amount * quantity
            dc = od.get("date_created", "")[:10]
            if dc:
                cell["first_seen"] = min(cell["first_seen"] or dc, dc)
                cell["last_seen"] = max(cell["last_seen"] or dc, dc)

    rows = sorted(by_sku.values(), key=lambda x: x["revenue_total"], reverse=True)
    return {
        "seller_id": seller_id,
        "recent_n_requested": recent_n,
        "packs_returned": len(packs),
        "orders_with_detail": len(order_details),
        "cache_hits": cache_hits,
        "skipped_429": skipped_429,
        "skipped_other": skipped_other,
        "unique_skus": len(rows),
        "rows": rows,
        "_note": "Token bucket: 80/min search, 1200/min detail. Order details cached in SQLite. Uses global_price (listing USD).",
    }


@app.get("/admin/cache-stats", dependencies=[Depends(require_service_token)])
async def admin_cache_stats():
    return await db.cache_stats()


# ---------- M3 → Feishu Bitable writer ----------

# Bitable target (created 2026-05-12)
FEISHU_BASE_APP_TOKEN = os.getenv("FEISHU_BASE_APP_TOKEN", "WM3LbBr76aRqMys2of8c1dGInEb")
FEISHU_BASE_TABLE_ID = os.getenv("FEISHU_BASE_TABLE_ID", "tbl09sRPkX35PDfU")

# Map child seller_id → store option label (must match Bitable single-select option)
SHOP_LABEL: dict[int, str] = {
    1510203792: "ML CBT-自发货 (1510203792)",
    1502236229: "ML CBT-FULL (1502236229)",
    1407362838: "ML 本土1店 FUNLABDIRECTMX",
    1436420028: "ML 本土2店 FUNLAB_MX",
}


async def _feishu_tenant_token() -> str:
    app_id = os.getenv("FEISHU_APP_ID", "cli_a9f6ae86fce8dbd8")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_secret:
        raise HTTPException(500, "FEISHU_APP_SECRET not configured")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
    if r.status_code != 200:
        raise HTTPException(502, f"feishu auth failed: {r.text}")
    return r.json()["tenant_access_token"]


def _to_ms(date_str: str | None) -> int | None:
    """ISO date string (YYYY-MM-DD or full ISO) → ms timestamp."""
    if not date_str:
        return None
    try:
        from datetime import datetime
        s = date_str[:10]
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return None


@app.post("/report/sync-feishu", dependencies=[Depends(require_service_token)])
async def report_sync_feishu(
    seller_id: int,
    recent_n: int = 100,
    parent_user_id: int = 1502520822,
    period_label: str = "",
):
    """Pull recent N orders, aggregate by SKU, write to Feishu Bitable."""
    if seller_id not in SHOP_LABEL:
        raise HTTPException(400, f"unknown seller_id {seller_id}; allowed: {list(SHOP_LABEL.keys())}")
    period = period_label or f"recent_{recent_n}"

    agg = await _report_sku_recent_impl(seller_id, recent_n, parent_user_id)
    rows = agg["rows"]
    if not rows:
        return {"status": "no_data", "agg": agg}

    feishu_token = await _feishu_tenant_token()
    import time as _t
    pulled_at_ms = int(_t.time() * 1000)

    records: list[dict] = []
    for r in rows:
        rev = r["revenue_total"]
        cnt = r["orders_count"]
        record: dict = {
            "fields": {
                "SKU": r["sku"],
                "平台": "Mercado Libre",
                "店铺": SHOP_LABEL[seller_id],
                "周期": period,
                "订单数": cnt,
                "件数": r["units"],
                "营收(USD)": round(rev, 2),
                "客单价(USD)": round(rev / cnt, 2) if cnt else 0,
                "商品标题": r.get("sample_title") or "",
                "数据拉取时间": pulled_at_ms,
            }
        }
        fs = _to_ms(r.get("first_seen"))
        ls = _to_ms(r.get("last_seen"))
        if fs:
            record["fields"]["首次销售日"] = fs
        if ls:
            record["fields"]["最后销售日"] = ls
        records.append(record)

    # batch insert
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_APP_TOKEN}/tables/{FEISHU_BASE_TABLE_ID}/records/batch_create",
            headers={"Authorization": f"Bearer {feishu_token}", "Content-Type": "application/json"},
            json={"records": records},
        )
    if r.status_code != 200 or r.json().get("code") != 0:
        raise HTTPException(502, f"feishu write failed: {r.status_code} {r.text[:500]}")

    return {
        "status": "synced",
        "seller_id": seller_id,
        "shop": SHOP_LABEL[seller_id],
        "period": period,
        "rows_written": len(records),
        "agg_summary": {k: agg[k] for k in ("packs_returned", "orders_with_detail", "unique_skus")},
        "bitable_url": f"https://u1wpma3xuhr.feishu.cn/base/{FEISHU_BASE_APP_TOKEN}",
    }


async def _report_sku_monthly_impl(seller_id: int, month: str, parent_user_id: int):
    row = await db.get_token(parent_user_id)
    if not row:
        raise HTTPException(404, "parent token not found in DB; seed first")
    headers = {"Authorization": f"Bearer {row['access_token']}"}

    yyyy, mm = month.split("-")
    yyyy, mm = int(yyyy), int(mm)
    date_from = f"{yyyy}-{mm:02d}-01T00:00:00.000-00:00"
    date_to_year, date_to_month = (yyyy + 1, 1) if mm == 12 else (yyyy, mm + 1)
    date_to = f"{date_to_year}-{date_to_month:02d}-01T00:00:00.000-00:00"

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. items SKU map
        items_meta = await _fetch_seller_items_with_sku(client, headers, seller_id)

        # 2. orders in month (marketplace endpoint)
        orders: list[dict] = []
        offset = 0
        while True:
            r = await _ml_get(
                client,
                "https://api.mercadolibre.com/marketplace/orders/search",
                headers,
                {
                    "seller": seller_id,
                    "order.date_created.from": date_from,
                    "order.date_created.to": date_to,
                    "limit": 50,
                    "offset": offset,
                    "sort": "date_asc",
                },
            )
            if r.status_code != 200:
                raise HTTPException(502, f"orders/search failed seller={seller_id} offset={offset} status={r.status_code} body={r.text[:400]}")
            try:
                rr = r.json().get("results", [])
            except Exception:
                raise HTTPException(502, f"orders/search non-JSON seller={seller_id} offset={offset} status={r.status_code} ct={r.headers.get('content-type')} body={r.text[:400]}")
            if not rr:
                break
            orders.extend(rr)
            if len(rr) < 50:
                break
            offset += 50

    # 3. aggregate by SKU
    agg: dict[str, dict] = {}
    untracked_items: dict[str, int] = {}
    for o in orders:
        for it in (o.get("config", {}).get("items") or []):
            item_id = it.get("id")
            meta = items_meta.get(item_id)
            if not meta:
                untracked_items[item_id] = untracked_items.get(item_id, 0) + 1
                continue
            sku = meta["sku"]
            cell = agg.setdefault(sku, {
                "sku": sku,
                "orders_count": 0,
                "list_price": meta.get("price"),
                "currency_id": meta.get("currency_id"),
                "sample_title": meta.get("title"),
                "item_ids": set(),
            })
            cell["orders_count"] += 1
            cell["item_ids"].add(item_id)

    rows = []
    for cell in agg.values():
        item_ids = sorted(cell.pop("item_ids"))
        cell["item_ids"] = item_ids
        cell["estimated_revenue"] = (cell["orders_count"] * (cell.get("list_price") or 0))
        rows.append(cell)
    rows.sort(key=lambda x: x["orders_count"], reverse=True)

    return {
        "seller_id": seller_id,
        "month": month,
        "orders_total": len(orders),
        "listings_total": len(items_meta),
        "skus_with_orders": len(rows),
        "untracked_items": untracked_items,
        "rows": rows,
        "_note": "estimated_revenue uses listing list_price × orders_count (rough). For exact revenue, fetch per-order detail (todo M3.1).",
    }
