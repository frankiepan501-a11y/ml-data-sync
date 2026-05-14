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


from fastapi.responses import RedirectResponse, HTMLResponse


@app.get("/oauth/start")
async def oauth_start(app: str, label: str = ""):
    """Self-serve OAuth kickoff — operators just click this link.

    Looks up the ml_apps row by app_key, builds the correct ML authorization URL
    (each account system has its own auth host), 302 redirects to ML login.
    """
    app_row = await db.get_app(app)
    if not app_row:
        raise HTTPException(404, f"app_key '{app}' not registered. POST to /admin/apps first.")
    import urllib.parse
    state = app  # state carries app_key back to callback
    if label:
        state = f"{app}|{label}"
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app_row["client_id"],
        "redirect_uri": app_row["redirect_uri"],
        "state": state,
        "scope": "offline_access read",
    })
    url = f"https://{app_row['auth_host']}/authorization?{qs}"
    return RedirectResponse(url=url, status_code=302)


@app.get("/oauth/apps-page", response_class=HTMLResponse)
async def oauth_apps_page():
    """Minimal HTML page listing registered Apps with click-to-authorize links."""
    apps = await db.list_apps()
    rows_html = "\n".join(
        f"<tr><td>{a['app_key']}</td><td>{a['app_name']}</td><td>{a['account_type']}</td>"
        f"<td>{a['auth_host']}</td>"
        f"<td><a href='/oauth/start?app={a['app_key']}'>授权该 App →</a></td></tr>"
        for a in apps
    )
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>ML 多店 OAuth 自助页</title>
<style>body{{font-family:sans-serif;padding:20px;max-width:900px;margin:auto}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#f4f4f4}}a{{color:#0066cc;text-decoration:none}}a:hover{{text-decoration:underline}}</style>
</head><body><h1>ML 多店 OAuth 自助页</h1>
<p>共 {len(apps)} 个 App。点最右侧链接即跳 ML 登录授权，自动写入服务端 tokens 表。</p>
<table><thead><tr><th>app_key</th><th>App 名</th><th>账号体系</th><th>auth host</th><th>操作</th></tr></thead>
<tbody>{rows_html}</tbody></table></body></html>"""


# ---------- OAuth callback ----------

async def _exchange_code(code: str, app_row: dict | None = None) -> dict:
    """Exchange authorization code → tokens. If app_row given, use that App's credentials.

    Fallback to legacy env vars (single-app mode) for backward compatibility.
    """
    if app_row:
        client_id = app_row["client_id"]
        client_secret = app_row["client_secret"]
        redirect_uri = app_row["redirect_uri"]
    else:
        client_id = os.getenv("ML_APP_ID")
        client_secret = os.getenv("ML_APP_SECRET")
        redirect_uri = os.getenv("ML_REDIRECT_URI")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.mercadolibre.com/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
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

    # state may carry "app_key" or "app_key|store_label"
    app_key, store_label = None, None
    if state:
        if "|" in state:
            app_key, store_label = state.split("|", 1)
        else:
            app_key = state

    app_row = await db.get_app(app_key) if app_key else None
    data = await _exchange_code(code, app_row=app_row)
    user_id = data.get("user_id")
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in") or 21600
    scope = data.get("scope")

    user_info = await _fetch_user_info(access_token) if access_token else {}
    nickname = user_info.get("nickname")
    site_id = user_info.get("site_id")

    # Default store_label from app_row if not provided in state
    if not store_label and app_row:
        store_label = app_row.get("store_label_default") or app_row.get("app_name")

    await db.upsert_token(
        user_id=user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scope=scope,
        nickname=nickname,
        site_id=site_id,
        app_key=app_key,
        store_label=store_label,
    )
    print(f"[OAUTH STORED] user_id={user_id} app_key={app_key} nickname={nickname} site={site_id} has_refresh={bool(refresh_token)}", flush=True)

    return {
        "status": "success",
        "user_id": user_id,
        "app_key": app_key,
        "store_label": store_label,
        "nickname": nickname,
        "site_id": site_id,
        "scope_has_offline_access": "offline_access" in (scope or ""),
        "has_refresh_token": bool(refresh_token),
        "expires_in": expires_in,
        "note": f"Token persisted as {store_label or nickname}. You can close this page.",
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
        app_key=body.get("app_key"),
        store_label=body.get("store_label"),
    )
    row = await db.get_token(int(body["user_id"]))
    return {"status": "seeded", "token": db.redact(row) if row else None}


async def _refresh_one(user_id: int) -> dict:
    row = await db.get_token(user_id)
    if not row:
        raise HTTPException(404, f"no token for user_id={user_id}")
    if not row.get("refresh_token"):
        raise HTTPException(400, f"user_id={user_id} has no refresh_token (re-authorize required)")

    # Use the App's client_id/secret that originally minted this token (per app_key),
    # falling back to env ML_APP_ID/SECRET for legacy tokens without app_key.
    client_id = os.getenv("ML_APP_ID")
    client_secret = os.getenv("ML_APP_SECRET")
    app_key = row.get("app_key")
    if app_key:
        app_row = await db.get_app(app_key)
        if app_row:
            client_id = app_row["client_id"]
            client_secret = app_row["client_secret"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.mercadolibre.com/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
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


@app.post("/admin/tokens/{user_id}/app-link", dependencies=[Depends(require_service_token)])
async def admin_token_app_link(user_id: int, app_key: str, store_label: str | None = None):
    """Link an existing token row to an ml_apps entry (sets app_key + store_label).
    Useful for backfilling tokens that were stored before multi-app system."""
    ok = await db.update_token_app_link(user_id, app_key, store_label)
    if not ok:
        raise HTTPException(404, f"token user_id={user_id} not found")
    row = await db.get_token(user_id)
    return {"status": "linked", "token": db.redact(row) if row else None}


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

@app.get("/admin/raw-ml-get", dependencies=[Depends(require_service_token)])
async def admin_raw_ml_get(user_id: int, path: str):
    """Temporary probe: GET any ML endpoint with the given user_id's token.

    Path examples (URL-encode the query):
      /billing/integration/periods?user_id=1510203792
      /billing/integration/periods/key/{KEY}/group/ML/details
      /advertising/advertisers/{id}/campaigns

    Used for exploring billing/advertising API shapes during route B build-out.
    """
    row = await db.get_token(user_id)
    if not row:
        raise HTTPException(404, f"token not found for user_id={user_id}")
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    if not path.startswith("/"):
        path = "/" + path
    url = f"https://api.mercadolibre.com{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
    return {
        "url": url,
        "status": r.status_code,
        "body": r.text[:4000] if r.headers.get("content-type", "").startswith("application/json") else r.text[:1000],
        "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "x-request-id", "retry-after")},
    }


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

    # CBT uses /marketplace/orders/... endpoints; local accounts use bare /orders/...
    is_cbt = (row.get("app_key") == "cbt")
    orders_search_url = (
        "https://api.mercadolibre.com/marketplace/orders/search" if is_cbt
        else "https://api.mercadolibre.com/orders/search"
    )
    orders_detail_prefix = (
        "https://api.mercadolibre.com/marketplace/orders" if is_cbt
        else "https://api.mercadolibre.com/orders"
    )

    async with httpx.AsyncClient(timeout=120) as client:
        # 1. pull pack list (last N) — orders/search is critical; allow ONE 30s retry on 429
        packs: list[dict] = []
        offset = 0
        while len(packs) < recent_n:
            page_size = min(50, recent_n - len(packs))
            params = {"seller": seller_id, "limit": page_size, "offset": offset, "sort": "date_desc"}
            r = await _ml_get(client, orders_search_url, headers, params)
            if r.status_code == 429:
                await _asyncio.sleep(30)
                r = await _ml_get(client, orders_search_url, headers, params)
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
            # CBT packs have inner `orders[]`; local search may return flat orders directly
            inner = pack.get("orders") or [pack]
            for sub in inner:
                order_id = sub.get("id")
                if not order_id:
                    continue
                # Cache check first — order details are immutable once paid
                cached = await db.cache_get_order(int(order_id))
                if cached and cached.get("_payload"):
                    order_details.append(cached["_payload"])
                    cache_hits += 1
                    continue
                rd = await _ml_get(client, f"{orders_detail_prefix}/{order_id}", headers)
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
            # CBT exposes item.global_price={currency,amount}; local exposes order_item.{unit_price, currency_id}
            gp = it.get("global_price") or {}
            if gp:
                currency = gp.get("currency") or "?"
                amount = float(gp.get("amount") or 0)
            else:
                currency = item.get("currency_id") or od.get("currency_id") or "?"
                amount = float(item.get("unit_price") or 0)
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


# ---------- ml_apps admin ----------

@app.get("/admin/apps", dependencies=[Depends(require_service_token)])
async def admin_list_apps():
    apps = await db.list_apps()
    return {"count": len(apps), "apps": [db.redact_app(a) for a in apps]}


@app.post("/admin/apps", dependencies=[Depends(require_service_token)])
async def admin_upsert_app(req: Request):
    """Register or update an ML App config.

    Body: {
      "app_key": "local_mx_1",
      "app_name": "Funlab Internal Data Sync · MX1",
      "client_id": "1234567890123456",
      "client_secret": "xxx",
      "account_type": "local_mx",
      "auth_host": "auth.mercadolibre.com.mx",   // optional, derived from account_type if absent
      "store_label_default": "ML 本土1店 FUNLABDIRECTMX",
      "redirect_uri": "https://ml-sync.zeabur.app/oauth/callback"  // optional, defaults to ML_REDIRECT_URI env
    }
    """
    body = await req.json()
    required = ("app_key", "app_name", "client_id", "client_secret", "account_type")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(400, f"missing fields: {missing}")

    # Sensible defaults for auth_host based on account_type
    default_hosts = {
        "cbt": "global-selling.mercadolibre.com",
        "local_mx": "auth.mercadolibre.com.mx",
        "local_br": "auth.mercadolivre.com.br",
        "local_ar": "auth.mercadolibre.com.ar",
        "local_cl": "auth.mercadolibre.cl",
        "local_co": "auth.mercadolibre.com.co",
        "local_pe": "auth.mercadolibre.com.pe",
    }
    auth_host = body.get("auth_host") or default_hosts.get(body["account_type"])
    if not auth_host:
        raise HTTPException(400, f"cannot infer auth_host for account_type={body['account_type']}; pass auth_host explicitly")
    redirect_uri = body.get("redirect_uri") or os.getenv("ML_REDIRECT_URI") or "https://ml-sync.zeabur.app/oauth/callback"

    await db.upsert_app(
        app_key=body["app_key"],
        app_name=body["app_name"],
        client_id=str(body["client_id"]),
        client_secret=body["client_secret"],
        auth_host=auth_host,
        redirect_uri=redirect_uri,
        account_type=body["account_type"],
        store_label_default=body.get("store_label_default"),
    )
    row = await db.get_app(body["app_key"])
    return {"status": "saved", "app": db.redact_app(row) if row else None,
            "next_step": f"Open https://ml-sync.zeabur.app/oauth/start?app={body['app_key']} in browser to authorize a seller account."}


# ---------- Phase 2·① ML Webhook ----------

@app.post("/webhook/ml")
async def webhook_ml(req: Request):
    """Receive ML notification, validate, enqueue, ACK 200 within ~500ms.

    ML expects HTTP 200 quickly (≤500ms); otherwise retries up to 8 times in 1 hour
    then marks as missed_feed. We must NOT process inline — just ACK + enqueue.

    Auth: verifies application_id matches our configured ML_APP_ID.
    """
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    # Multi-app validation: accept if application_id matches ANY registered ml_apps row,
    # OR the legacy single-App ML_APP_ID env (backward compat).
    incoming_app_id = str(body.get("application_id") or "")
    legacy_app_id = os.getenv("ML_APP_ID", "")
    is_known = (incoming_app_id == legacy_app_id) if legacy_app_id else False
    if not is_known and incoming_app_id:
        app_row = await db.get_app_by_client_id(incoming_app_id)
        is_known = bool(app_row)
    if not is_known:
        return {"status": "ignored", "reason": f"unknown application_id {incoming_app_id}"}
    is_new = await db.enqueue_event(body)
    return {"status": "queued" if is_new else "duplicate", "topic": body.get("topic"), "resource": body.get("resource")}


@app.get("/admin/event-queue-stats", dependencies=[Depends(require_service_token)])
async def admin_event_queue_stats():
    return await db.event_queue_stats()


@app.post("/admin/process-webhook-queue", dependencies=[Depends(require_service_token)])
async def admin_process_webhook_queue(limit: int = 50, parent_user_id: int = 1502520822):
    """Drain up to `limit` pending events.

    For each event:
      - GET the resource URL with parent token
      - On orders_v2: cache the order detail
      - On items: cache the item detail
      - Other topics currently just marked done (extend later)

    Run via n8n cron every 5 minutes.
    """
    row = await db.get_token(parent_user_id)
    if not row:
        raise HTTPException(404, "parent token not found")
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    events = await db.claim_pending_events(limit)
    if not events:
        return {"processed": 0, "events": []}

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for ev in events:
            topic = ev["topic"] or ""
            resource = ev["resource"] or ""
            # Decide URL + cache target by resource path (more robust than topic name)
            # ML 2026 UI uses topics: items / marketplace_items / marketplace_questions /
            #   marketplace_orders / marketplace_shipments / marketplace_orders_on_site / ...
            # resource path is canonical (e.g. /orders/<id>, /items/<id>, /questions/<id>)
            is_order = resource.startswith("/orders/") or "orders" in topic
            is_item = resource.startswith("/items/") or topic == "items"
            # For CBT parent token: orders need /marketplace prefix to be readable
            url = f"https://api.mercadolibre.com{resource}"
            if is_order and "/marketplace/" not in url:
                url = f"https://api.mercadolibre.com/marketplace{resource}"
            try:
                r = await _ml_get(client, url, headers)
                if r.status_code == 200:
                    detail = r.json()
                    if is_order:
                        seller_id = ((detail.get("seller") or {}).get("id")
                                     or (detail.get("orders") or [{}])[0].get("seller", {}).get("id")
                                     or ev.get("user_id") or 0)
                        await db.cache_put_order(int(detail.get("id") or 0), int(seller_id or 0), detail)
                    elif is_item:
                        await db.cache_put_item(detail.get("id"), detail)
                    # other topics (questions/shipments/messages): payload not cached yet, just mark done
                    await db.mark_event_done(ev["id"])
                    results.append({"id": ev["id"], "ok": True, "topic": topic, "resource": resource})
                else:
                    await db.mark_event_failed(ev["id"], f"http {r.status_code}: {r.text[:200]}")
                    results.append({"id": ev["id"], "ok": False, "http": r.status_code, "resource": resource})
            except Exception as e:
                await db.mark_event_failed(ev["id"], f"{type(e).__name__}: {str(e)[:200]}")
                results.append({"id": ev["id"], "ok": False, "error": str(e)[:100]})

    ok = sum(1 for r in results if r.get("ok"))
    return {"processed": len(events), "ok": ok, "failed": len(events) - ok, "events": results}


@app.post("/admin/missed-feeds", dependencies=[Depends(require_service_token)])
async def admin_missed_feeds(parent_user_id: int = 1502520822):
    """Backfill via ML missed_feeds endpoint — for events ML retried 8x without our 200.

    Run daily via n8n cron.
    """
    row = await db.get_token(parent_user_id)
    if not row:
        raise HTTPException(404, "parent token not found")
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    app_id = os.getenv("ML_APP_ID")
    if not app_id:
        raise HTTPException(500, "ML_APP_ID not configured")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await _ml_get(client, f"https://api.mercadolibre.com/missed_feeds?app_id={app_id}", headers)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"missed_feeds failed: {r.text[:300]}")
    missed = r.json() if isinstance(r.json(), list) else r.json().get("missed_feeds") or []
    enqueued = 0
    for n in missed:
        if await db.enqueue_event(n):
            enqueued += 1
    return {"missed_total": len(missed), "newly_enqueued": enqueued}


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
    2378517428: "ML 巴西本土店 AIRSOFT COMERCIAL",
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
        currency = r.get("currency") or "?"
        record: dict = {
            "fields": {
                "SKU": r["sku"],
                "平台": "Mercado Libre",
                "店铺": SHOP_LABEL[seller_id],
                "周期": period,
                "订单数": cnt,
                "件数": r["units"],
                "币种": currency,
                "营收(原币)": round(rev, 2),
                "客单价(原币)": round(rev / cnt, 2) if cnt else 0,
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


@app.post("/report/sync-feishu-monthly", dependencies=[Depends(require_service_token)])
async def report_sync_feishu_monthly(seller_id: int, month: str, period_label: str = ""):
    """Aggregate seller_id's `month` orders FROM SQLite CACHE + Lingxing cost/FX → Feishu.

    Reads ml_order_cache, enriches with Lingxing cg_price (RMB cost) and monthly FX rate
    to compute 营收(RMB), 采购成本(RMB), 简易毛利(RMB). Does NOT call ML.

    Args:
      seller_id: ML child seller id (must be in SHOP_LABEL)
      month: YYYY-MM
      period_label: optional Feishu 周期 label; default = "month_{month}"
    """
    if seller_id not in SHOP_LABEL:
        raise HTTPException(400, f"unknown seller_id {seller_id}; allowed: {list(SHOP_LABEL.keys())}")
    period = period_label or f"month_{month}"

    cached_rows = await db.cache_list_orders_for_month(seller_id, month)
    if not cached_rows:
        return {"status": "no_cache", "seller_id": seller_id, "month": month,
                "hint": "Run /admin/backfill-orders to fill cache, or wait for webhook to populate."}

    # Aggregate by SKU — mirrors _report_sku_recent_impl logic, dual-schema aware
    # Phase B1: also extract order_items[].sale_fee (ML commission in seller currency)
    by_sku: dict[str, dict] = {}
    for cr in cached_rows:
        od = cr.get("_payload") or {}
        for item in (od.get("order_items") or []):
            it = item.get("item") or {}
            sku = it.get("seller_sku") or it.get("seller_custom_field") or "(no_sku)"
            gp = it.get("global_price") or {}
            if gp:
                currency = gp.get("currency") or "?"
                amount = float(gp.get("amount") or 0)
            else:
                currency = item.get("currency_id") or od.get("currency_id") or "?"
                amount = float(item.get("unit_price") or 0)
            quantity = int(item.get("quantity") or 1)
            sale_fee = float(item.get("sale_fee") or 0)  # ML commission per line item (seller currency)
            cell = by_sku.setdefault(sku, {
                "sku": sku, "orders_count": 0, "units": 0, "revenue_total": 0.0,
                "commission_total": 0.0,
                "currency": currency, "sample_title": it.get("title"),
                "first_seen": None, "last_seen": None,
            })
            cell["orders_count"] += 1
            cell["units"] += quantity
            cell["revenue_total"] += amount * quantity
            cell["commission_total"] += sale_fee  # sale_fee is already per-line total
            dc = (od.get("date_created") or "")[:10]
            if dc:
                cell["first_seen"] = min(cell["first_seen"] or dc, dc)
                cell["last_seen"] = max(cell["last_seen"] or dc, dc)

    rows = sorted(by_sku.values(), key=lambda x: x["revenue_total"], reverse=True)
    if not rows:
        return {"status": "no_data", "seller_id": seller_id, "month": month,
                "cached_orders": len(cached_rows)}

    # Enrich with Lingxing cost (cg_price) + monthly FX rate → compute RMB revenue/cost/profit
    from app import lingxing, advertising
    try:
        products = await lingxing.fetch_all_products()
        fx_map = await lingxing.fetch_fx_rate(month)
    except Exception as e:
        products, fx_map = {}, {}
        lingxing_error = str(e)[:200]
    else:
        lingxing_error = None

    # Phase B1: pull advertising spend for this seller's advertiser_id, attribute to SKUs by name
    ad_sku_cost: dict[str, float] = {}
    ad_unallocated_cost = 0.0
    ad_currency = "?"
    ad_advertiser_id = advertising.ADVERTISER_BY_SELLER.get(seller_id)
    ad_token_user = advertising.TOKEN_USER_FOR_ADVERTISING.get(seller_id, seller_id)
    if ad_advertiser_id:
        try:
            campaigns = await advertising.fetch_campaigns_for_month(ad_advertiser_id, month, ad_token_user)
            known_skus = {r["sku"] for r in rows}
            ad_sku_cost, ad_unallocated_cost = advertising.attribute_ad_cost_to_skus(campaigns, known_skus)
            ad_currency = advertising.AD_CURRENCY_BY_ADVERTISER.get(ad_advertiser_id, "?")
        except Exception:
            pass

    # VAT rate by site_id (from any cached order's currency context: MLM/MLB/CBT)
    # Use first cached row to infer site_id; CBT uses USD revenue but seller site is CBT
    site_id_for_vat = "?"
    for cr in cached_rows:
        site = ((cr.get("_payload") or {}).get("seller") or {}).get("site_id")
        if not site:
            # fallback to currency-based heuristic
            cur = cr.get("currency") or (cr.get("_payload") or {}).get("currency_id")
            site = {"USD": "CBT", "MXN": "MLM", "BRL": "MLB"}.get(cur, "?")
        site_id_for_vat = site
        break
    vat_rate = advertising.vat_for_site(site_id_for_vat)

    feishu_token = await _feishu_tenant_token()
    import time as _t
    pulled_at_ms = int(_t.time() * 1000)
    records: list[dict] = []
    skus_missing_cost: list[str] = []
    for r in rows:
        rev = r["revenue_total"]; cnt = r["orders_count"]
        currency = r.get("currency") or "?"
        commission_local = r.get("commission_total") or 0  # sale_fee, seller currency
        ad_cost_local = ad_sku_cost.get(r["sku"], 0.0)  # ads cost, advertiser currency (usually seller-side)
        vat_estimate_local = rev * vat_rate
        fields: dict = {
            "SKU": r["sku"], "平台": "Mercado Libre", "店铺": SHOP_LABEL[seller_id],
            "周期": period, "订单数": cnt, "件数": r["units"],
            "币种": currency,
            "营收(原币)": round(rev, 2),
            "客单价(原币)": round(rev / cnt, 2) if cnt else 0,
            "ML佣金(原币)": round(commission_local, 2),
            "广告费(原币)": round(ad_cost_local, 2),
            "VAT估算(原币)": round(vat_estimate_local, 2),
            "商品标题": r.get("sample_title") or "",
            "数据拉取时间": pulled_at_ms,
        }
        # FX-aware RMB columns
        fx = fx_map.get(currency)
        if fx:
            fields["我的汇率"] = round(fx, 4)
            rev_rmb = rev * fx
            commission_rmb = commission_local * fx
            ad_cost_rmb = ad_cost_local * fx  # ad cost in advertiser currency, assumed = seller currency (verified for MLM/MLB)
            vat_rmb = vat_estimate_local * fx
            fields["营收(RMB)"] = round(rev_rmb, 2)
            fields["ML佣金(RMB)"] = round(commission_rmb, 2)
            fields["广告费(RMB)"] = round(ad_cost_rmb, 2)
            fields["VAT估算(RMB)"] = round(vat_rmb, 2)
            # cg_price from Lingxing (RMB, no FX needed)
            prod = products.get(r["sku"])
            if prod and prod.get("cg_price") is not None:
                try:
                    cgp = float(prod["cg_price"])
                    cost_rmb = cgp * r["units"]
                    fields["采购成本(RMB)"] = round(cost_rmb, 2)
                    fields["简易毛利(RMB)"] = round(rev_rmb - cost_rmb, 2)
                    # Phase B1 全额毛利 = 简易毛利 - ML佣金 - 广告费 - VAT估算 (all RMB)
                    full_profit = rev_rmb - cost_rmb - commission_rmb - ad_cost_rmb - vat_rmb
                    fields["全额毛利(RMB)"] = round(full_profit, 2)
                except (TypeError, ValueError):
                    skus_missing_cost.append(r["sku"])
            else:
                skus_missing_cost.append(r["sku"])
        fs = _to_ms(r.get("first_seen")); ls = _to_ms(r.get("last_seen"))
        if fs: fields["首次销售日"] = fs
        if ls: fields["最后销售日"] = ls
        records.append({"fields": fields})

    # If unallocated ad spend > 0, emit a synthetic row
    if ad_unallocated_cost > 0:
        unalloc_fx = fx_map.get(ad_currency) or 0
        unalloc_rmb = ad_unallocated_cost * unalloc_fx if unalloc_fx else 0
        records.append({"fields": {
            "SKU": "_unallocated_ads",
            "平台": "Mercado Libre",
            "店铺": SHOP_LABEL[seller_id],
            "周期": period,
            "订单数": 0, "件数": 0,
            "币种": ad_currency,
            "营收(原币)": 0,
            "广告费(原币)": round(ad_unallocated_cost, 2),
            "广告费(RMB)": round(unalloc_rmb, 2),
            "我的汇率": round(unalloc_fx, 4) if unalloc_fx else 0,
            "商品标题": "未归因广告花费 (campaign 名未含已知 SKU)",
            "数据拉取时间": pulled_at_ms,
        }})

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_APP_TOKEN}/tables/{FEISHU_BASE_TABLE_ID}/records/batch_create",
            headers={"Authorization": f"Bearer {feishu_token}", "Content-Type": "application/json"},
            json={"records": records},
        )
    if r.status_code != 200 or r.json().get("code") != 0:
        raise HTTPException(502, f"feishu write failed: {r.status_code} {r.text[:500]}")

    return {"status": "synced", "seller_id": seller_id, "shop": SHOP_LABEL[seller_id],
            "month": month, "period": period, "rows_written": len(records),
            "cached_orders_total": len(cached_rows), "unique_skus": len(rows),
            "lingxing_products_loaded": len(products),
            "lingxing_error": lingxing_error,
            "skus_missing_cost": skus_missing_cost,
            "advertiser_id": ad_advertiser_id,
            "ad_currency": ad_currency,
            "ad_attributed_skus": len(ad_sku_cost),
            "ad_unallocated_local": round(ad_unallocated_cost, 2),
            "vat_rate": vat_rate,
            "site_id_inferred": site_id_for_vat,
            "bitable_url": f"https://u1wpma3xuhr.feishu.cn/base/{FEISHU_BASE_APP_TOKEN}"}


@app.post("/admin/backfill-orders", dependencies=[Depends(require_service_token)])
async def admin_backfill_orders(seller_id: int, recent_n: int = 200, parent_user_id: int = 0):
    """One-time historical fill: pull recent N orders into ml_order_cache, no Feishu write.

    Useful for filling pre-webhook history before running /report/sync-feishu-monthly.
    Reuses _report_sku_recent_impl which already caches detail responses.
    """
    parent = parent_user_id or seller_id
    agg = await _report_sku_recent_impl(seller_id, recent_n, parent)
    # Discard aggregation; the side-effect of cache_put_order is what we want.
    return {"status": "backfilled", "seller_id": seller_id, "parent_user_id": parent,
            "packs": agg.get("packs_returned"), "orders_with_detail": agg.get("orders_with_detail"),
            "unique_skus": agg.get("unique_skus"),
            "note": "Orders cached in SQLite. Now call /report/sync-feishu-monthly to write Feishu."}


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
