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

async def _fetch_seller_items_with_sku(client: httpx.AsyncClient, headers: dict, seller_id: int) -> dict[str, dict]:
    """Return {item_id: {sku, title, price, currency, status}} for all of a seller's listings."""
    items: dict[str, dict] = {}
    offset = 0
    while True:
        r = await client.get(
            f"https://api.mercadolibre.com/marketplace/users/{seller_id}/items/search",
            headers=headers,
            params={"limit": 50, "offset": offset},
        )
        ids = r.json().get("results", [])
        if not ids:
            break
        for item_id in ids:
            r2 = await client.get(
                f"https://api.mercadolibre.com/items/{item_id}",
                headers=headers,
            )
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


@app.get("/report/sku-monthly", dependencies=[Depends(require_service_token)])
async def report_sku_monthly(
    seller_id: int,
    month: str,
    parent_user_id: int = 1502520822,
):
    """SKU × month aggregation PoC.

    Args:
        seller_id: child seller user_id (e.g. 1510203792 = CBT 自发货)
        month: "YYYY-MM" (e.g. "2026-04")
        parent_user_id: CBT parent user (default 1502520822, owner of token in DB)
    """
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
            r = await client.get(
                "https://api.mercadolibre.com/marketplace/orders/search",
                headers=headers,
                params={
                    "seller": seller_id,
                    "order.date_created.from": date_from,
                    "order.date_created.to": date_to,
                    "limit": 50,
                    "offset": offset,
                    "sort": "date_asc",
                },
            )
            rr = r.json().get("results", [])
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
