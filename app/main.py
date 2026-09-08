"""ML Direct Sync — FastAPI entrypoint.

M2: tokens persisted to SQLite; admin endpoints for seed/list/refresh.
"""

import os
import json
import math
import re
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, HTTPException, Request, Depends, Header, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
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
    return {
        "ok": True,
        "ad_sync_fail_closed": True,
        "ad_sync_failure_visible": True,
        "ad_sync_failure_durable": True,
        "ad_sync_failure_restart_safe": True,
        "ad_sync_preview": True,
        "ml_unified_report_generator": True,
        "ml_product_mapping_fail_closed": True,
        "ml_month_period_safe_20260907": True,
        "ml_month_numeric_normalized_20260907": True,
        "ml_month_cache_reconciled_20260907": True,
        "ml_month_scope_authoritative_20260907": True,
        "ml_month_ab_gate_strict_20260907": True,
        "ml_month_order_detail_refreshed_20260907": True,
        "ml_month_ab_gate_persisted_20260907": True,
        "ml_month_refresh_clock_skew_20260907": True,
        "ml_month_detail_failure_visible_20260907": True,
        "ml_month_partial_detail_accepted_20260907": True,
        "ml_month_partial_financial_fields_guarded_20260907": True,
        "ml_month_ab_report_hash_20260907": True,
        "ml_unified_ab_gate_20260907": True,
        "ml_month_purchase_cost_fail_closed_20260907": True,
        "ml_month_financial_numeric_fail_closed_20260907": True,
        "ml_month_live_ab_hash_guard_20260907": True,
        "ml_month_scope_replace_guard_20260907": True,
        "ml_unified_source_hash_guard_20260907": True,
        "ml_month_date_closed_scope_20260908": True,
        "ml_month_shipping_complete_20260908": True,
        "ml_shipping_fixed_site_currency_20260908": True,
        "ml_month_billing_adjustments_20260908": True,
        "ml_billing_first_page_cursor_20260908": True,
        "ml_billing_remaining_total_20260908": True,
        "ml_month_manual_logistics_preserve_20260908": True,
    }


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

@app.post("/admin/sku-audit", dependencies=[Depends(require_service_token)])
async def admin_sku_audit(month: str | None = None):
    """Audit ML SKU vs Lingxing ERP SKU vs alias map. Returns list of unmapped SKUs.

    Lightweight: reads from ml_order_cache (no new ML calls). For each seller, lists
    distinct seller_sku from cached orders within `month` (default = current month),
    cross-references against Lingxing productList + alias map. Returns SKUs that:
      - appear in cached ML orders
      - but NOT in Lingxing productList (after alias resolution)

    Intended as weekly n8n cron target →飞书 alert.
    """
    from datetime import date
    if not month:
        today = date.today()
        month = f"{today.year}-{today.month:02d}"

    from app import lingxing as lx
    products = await lx.fetch_all_products()
    erp_skus = set(products.keys())

    import time as _t
    result = {"month": month, "checked_at": int(_t.time()), "by_seller": {}, "total_unmapped": 0}
    for seller_id, label in SHOP_LABEL.items():
        cached = await db.cache_list_orders_for_month(seller_id, month)
        ml_skus_in_use: dict[str, int] = {}  # sku → unit count
        for cr in cached:
            od = cr.get("_payload") or {}
            for item in (od.get("order_items") or []):
                it = item.get("item") or {}
                sku = it.get("seller_sku") or it.get("seller_custom_field") or ""
                if not sku:
                    continue
                qty = int(item.get("quantity") or 1)
                ml_skus_in_use[sku] = ml_skus_in_use.get(sku, 0) + qty

        unmapped: list[dict] = []
        in_alias: list[dict] = []
        clean: int = 0
        for sku, qty in ml_skus_in_use.items():
            erp_sku = lx.resolve_erp_sku(sku)
            if erp_sku == sku and sku in erp_skus:
                clean += 1
                continue
            if erp_sku != sku and erp_sku in erp_skus:
                in_alias.append({"ml_sku": sku, "resolved_erp_sku": erp_sku, "units": qty})
                continue
            # NEW unmapped — needs attention
            unmapped.append({"ml_sku": sku, "units": qty})

        result["by_seller"][str(seller_id)] = {
            "shop": label,
            "ml_skus_total": len(ml_skus_in_use),
            "clean_match": clean,
            "in_alias_table": len(in_alias),
            "alias_details": in_alias,
            "unmapped_count": len(unmapped),
            "unmapped_skus": unmapped,
        }
        result["total_unmapped"] += len(unmapped)
    return result


@app.post("/admin/sku-audit-alert", dependencies=[Depends(require_service_token)])
async def admin_sku_audit_alert(month: str | None = None):
    """Audit + send Feishu alert if unmapped SKUs found. n8n cron target."""
    audit = await admin_sku_audit(month=month)
    total = audit.get("total_unmapped", 0)
    if total == 0:
        return {"status": "ok", "total_unmapped": 0, "message": "All ML SKUs mapped"}

    # Build alert message
    lines = [f"⚠️ ML SKU 审计发现 {total} 个未映射 SKU ({audit['month']})", ""]
    for sid, info in audit["by_seller"].items():
        if info["unmapped_count"] == 0:
            continue
        lines.append(f"【{info['shop']}】未映射 {info['unmapped_count']} 个:")
        for u in info["unmapped_skus"]:
            lines.append(f"  - {u['ml_sku']} ({u['units']} 件)")
        lines.append("")
    lines.append("→ 处理：让运营在 ML 后台改 seller_sku 字段成对应 ERP_SKU，或私聊 Claude 加 alias map")
    msg_text = "\n".join(lines)

    # Send to Frankie + 俊辉
    feishu_token = await _feishu_tenant_token()
    import httpx
    sent = []
    for receiver in ("ou_629ce01f4bc31de078e10fcb038dbf78",   # Frankie
                     "ou_b9dd2272e72908fe68964d7bba53109f"):  # 俊辉
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                    headers={"Authorization": f"Bearer {feishu_token}", "Content-Type": "application/json"},
                    json={"receive_id": receiver, "msg_type": "text",
                          "content": json.dumps({"text": msg_text}, ensure_ascii=False)},
                )
                sent.append({"receiver": receiver, "ok": r.status_code == 200,
                             "message_id": (r.json().get("data") or {}).get("message_id") if r.status_code == 200 else None})
        except Exception as e:
            sent.append({"receiver": receiver, "ok": False, "error": str(e)[:100]})

    return {"status": "alerted", "total_unmapped": total, "audit": audit, "feishu_sent": sent}


@app.get("/admin/debug-shipping-cache", dependencies=[Depends(require_service_token)])
async def admin_debug_shipping_cache(seller_id: int | None = None, shipment_id: int | None = None):
    """Cache-only probe: inspect ml_shipping_cache. No ML calls.

    ?shipment_id=X            -> the single cached row (incl payload)
    ?seller_id=Y              -> per-seller summary: how many rows have
                                 sender_cost<=0 (dirty/unsettled) vs >0
    """
    if shipment_id:
        return {"shipment_id": shipment_id, "row": await db.cache_get_shipping(shipment_id)}
    if seller_id:
        rows = await db.cache_list_shipping_for_seller(seller_id)
        zero = [r for r in rows if (r.get("sender_cost") or 0) <= 0]
        pos = [r for r in rows if (r.get("sender_cost") or 0) > 0]
        return {
            "seller_id": seller_id,
            "total_rows": len(rows),
            "sender_cost_zero_or_neg": len(zero),
            "sender_cost_positive": len(pos),
            "sample_zero": zero[:10],
            "sample_pos": pos[:5],
        }
    return {"error": "pass seller_id or shipment_id"}


@app.get("/admin/debug-sku-cache", dependencies=[Depends(require_service_token)])
async def admin_debug_sku_cache(seller_id: int, sku: str, month: str):
    """Debug: list cached orders for a (seller, month) containing the given SKU,
    showing per-order paid_amount, units, transaction_amount_refunded."""
    cached_rows = await db.cache_list_orders_for_month(seller_id, month)
    matches = []
    for cr in cached_rows:
        od = cr.get("_payload") or {}
        for item in (od.get("order_items") or []):
            it = item.get("item") or {}
            sku_v = it.get("seller_sku") or it.get("seller_custom_field") or ""
            if sku != "*" and sku_v != sku:
                continue
            payments = od.get("payments") or []
            matches.append({
                "order_id": od.get("id"),
                "sku": sku_v,
                "date_created": od.get("date_created"),
                "status": od.get("status"),
                "paid_amount": od.get("paid_amount"),
                "total_amount": od.get("total_amount"),
                "currency_id": od.get("currency_id"),
                "item_quantity": item.get("quantity"),
                "unit_price": item.get("unit_price"),
                "sale_fee": item.get("sale_fee"),
                "discounts": item.get("discounts"),  # P2.5 probe
                "_item_keys": sorted(item.keys()),  # P2.5 probe
                "_payload_size": len(str(od)),  # P2.5 probe (rough)
                "fetched_at": cr.get("fetched_at"),  # P2.5 probe
                "shipping_id": (od.get("shipping") or {}).get("id"),
                "payments_refunded": [p.get("transaction_amount_refunded") for p in payments],
                "payments_transaction_amount": [p.get("transaction_amount") for p in payments],
                "payments_status": [p.get("status") for p in payments],
            })
            break
    total_refunded = sum(sum(m["payments_refunded"] or [0]) for m in matches)
    return {
        "seller_id": seller_id, "sku": sku, "month": month,
        "matched_orders": len(matches),
        "total_refunded_summed": total_refunded,
        "orders": matches[:200],
    }


@app.get("/admin/raw-ml-get", dependencies=[Depends(require_service_token)])
async def admin_raw_ml_get(user_id: int, path: str, api_version: str | None = None, max_bytes: int = 6000):
    """Temporary probe: GET any ML endpoint with the given user_id's token.

    api_version: optional, sent as 'Api-Version' / 'api-version' header. ML's marketplace
    advertising endpoints require api-version: 2.

    Path examples (URL-encode the query):
      /billing/integration/periods?user_id=1510203792
      /marketplace/advertising/MLM/advertisers/{id}/product_ads/campaigns/search
    """
    row = await db.get_token(user_id)
    if not row:
        raise HTTPException(404, f"token not found for user_id={user_id}")
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    if api_version:
        headers["api-version"] = api_version
    if not path.startswith("/"):
        path = "/" + path
    url = f"https://api.mercadolibre.com{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
    return {
        "url": url,
        "status": r.status_code,
        "body": r.text[:max_bytes] if r.headers.get("content-type", "").startswith("application/json") else r.text[:1500],
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


# ============ A 引擎: CBT 平台P&L 从 ML API 直算 (对账官方 Orders report, 2026-06-17) ============
# 官方导出口径: S(净受领/回款) = K(收入) - L(佣金) - O(运费) - Q(税) - M(汇率差) - R(退款)
#   K = order_item.unit_price*qty (=官方Income per product, 已折后净额) / L = sale_fee (=Selling fee)
#   O = /marketplace/shipments/{id}/costs senders[].cost (=Shipping cost, 走 shipping.fetch_shipping_cost)
#   R = payments[].transaction_amount_refunded (CBT为MXN买家币→/base_exchange_rate折USD)
#   Q = K*CBT_TAX_RATE (CBT-MX税代扣无API, 实测~13.8%) / M = K*CBT_FX_RATE (汇率差结算端算, 粗估~0.18%)
#   🚨 discounts.amounts.seller 不是成本(K已折后), 绝不扣! (这是历史-1.8万假账根源)
# 缓存可续跑: 重复调直到 complete=true(pending=0)。GMT-6月边界 + date_created(无"order."前缀!) + 半月窗口避offset1000上限。
CBT_TAX_RATE = float(os.getenv("CBT_TAX_RATE", "0.138"))
CBT_FX_RATE = float(os.getenv("CBT_FX_RATE", "0.0018"))


@app.get("/report/cbt-pnl-api", dependencies=[Depends(require_service_token)])
async def cbt_pnl_api(seller_id: int, month: str, parent_user_id: int = 1502520822,
                      max_detail_fetch: int = 500, max_ship_fetch: int = 500):
    from app import shipping as _ship
    import calendar as _cal
    row = await db.get_token(parent_user_id)
    if not row:
        raise HTTPException(404, "parent token not found")
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    yyyy, mm = [int(x) for x in month.split("-")]
    _ = _cal  # (windows 不再需要; 纯从缓存算避免 orders/search 限流)
    # 🚨 纯从已缓存订单详情算(backfill/webhook 已缓存), 不调 orders/search → 无 429。
    # 缓存补全靠 /admin/backfill-orders?month=(date 参数已修); 本端点只读缓存 + 拉运费(capped 可续跑)。
    cached_orders = await db.cache_list_orders_for_month(seller_id, month)
    agg: dict = {}
    tot = dict(K=0.0, L=0.0, O=0.0, Q=0.0, M=0.0, R=0.0, units=0, orders=0, cancelled=0)
    new_detail = 0; new_ship = 0; pending_detail = 0; pending_ship = 0
    async with httpx.AsyncClient(timeout=120) as client:
        for cr in cached_orders:
            od = cr.get("_payload")
            if not od:
                continue
            tot["orders"] += 1
            if od.get("status") == "cancelled":
                tot["cancelled"] += 1
            items = od.get("order_items") or []
            # base_exchange_rate(MXN/USD)用于把买家币退款折USD
            ber = 0.0
            for it in items:
                ber = float(it.get("base_exchange_rate") or 0) or ber
            refunded_buyer = sum(float(p.get("transaction_amount_refunded") or 0) for p in (od.get("payments") or []))
            R_usd = (refunded_buyer / ber) if ber else 0.0
            ship_id = (od.get("shipping") or {}).get("id")
            O_usd = 0.0
            if ship_id:
                cs = await db.cache_get_shipping(int(ship_id))
                if cs and float(cs.get("sender_cost") or 0) > 0:
                    O_usd = float(cs["sender_cost"])
                elif new_ship < max_ship_fetch:
                    sc = await _ship.fetch_shipping_cost(int(ship_id), seller_id, int(od.get("id") or 0), parent_user_id, client)
                    new_ship += 1
                    if sc:
                        O_usd = float(sc.get("sender_cost") or 0)
                else:
                    pending_ship += 1
            tot["O"] += O_usd; tot["R"] += R_usd
            for it in items:
                item = it.get("item") or {}
                sku = item.get("seller_sku") or item.get("seller_custom_field") or "(no_sku)"
                qty = int(it.get("quantity") or 1)
                K = float(it.get("unit_price") or 0) * qty
                # 🚨 CBT /marketplace/orders/ 的 sale_fee 是 PER-LINE 总额(实测 order 2000016704287954
                # qty=2/unit_price=4.8/sale_fee=0.72=整行2单佣金, =导出L佣金), 不乘qty! (本土店原生
                # /orders/ 的 sale_fee 才是 per-unit 要×qty — 两端点语义相反, 别混)
                L = float(it.get("sale_fee") or 0)
                Q = K * CBT_TAX_RATE; M = K * CBT_FX_RATE
                a = agg.setdefault(sku, dict(K=0.0, L=0.0, Q=0.0, M=0.0, units=0))
                a["K"] += K; a["L"] += L; a["Q"] += Q; a["M"] += M; a["units"] += qty
                tot["K"] += K; tot["L"] += L; tot["Q"] += Q; tot["M"] += M; tot["units"] += qty
        S = tot["K"] - tot["L"] - tot["O"] - tot["Q"] - tot["M"] - tot["R"]
    return {
        "seller_id": seller_id, "month": month, "scope": "官方Orders report口径(全状态, R冲销取消单)",
        "cached_orders": len(cached_orders), "orders_processed": tot["orders"], "cancelled": tot["cancelled"],
        "pending_ship": pending_ship,
        "new_detail_fetched": new_detail, "new_ship_fetched": new_ship,
        "complete": pending_detail == 0 and pending_ship == 0,
        "totals_usd": {**{k: round(v, 2) for k, v in tot.items()}, "S_net_payback": round(S, 2)},
        "per_sku_top": {s: {k: round(v, 2) for k, v in a.items()}
                        for s, a in sorted(agg.items(), key=lambda x: -x[1]["K"])[:30]},
        "rates": {"tax": CBT_TAX_RATE, "fx": CBT_FX_RATE},
    }


@app.post("/report/cbt-ingest", dependencies=[Depends(require_service_token)])
async def cbt_ingest(month: str, commit: bool = False, fx: float = 6.8628,
                     folder_token: str | None = None,
                     preserve_existing_as: str | None = None,
                     finalize: bool = False):
    """CBT-FULL 官方导出(B)解析 → 按SKU update 飞书报表(task3云化, 2026-06-18).
    俊辉每月把3导出(Orders+账单+广告)传飞书云盘文件夹(env CBT_EXPORT_FOLDER_TOKEN), 本端点下载解析.
    month 必须显式传入. 默认 commit=false dry-run; commit=true 才写飞书。
    存在不同的旧版时，必须传 preserve_existing_as 保留旧版证据。
    finalize=true 仅能在 A/B 对账通过后使用。"""
    import traceback
    from app import cbt_ingest as _ci
    try:
        if commit:
            from app import ml_close
            await ml_close.invalidate_ab_verification(f"month_{month}", "cbt_ingest")
        result = await _ci.run(
            month,
            commit=commit,
            fx=fx,
            folder_token=folder_token,
            preserve_existing_as=preserve_existing_as,
        )
        if result.get("status") != "ok":
            raise HTTPException(status_code=422, detail=result)
        if commit and finalize and result.get("status") == "ok":
            from app import meitong_cost, ml_close
            period = f"month_{month}"
            cost = await run_in_threadpool(meitong_cost.run, period, 12, True)
            close = await ml_close.audit(
                period=period,
                commit=True,
                run_cost_preview=False,
                cost_summary=cost,
                ab_verified=True,
            )
            result["post_ingest"] = {"sync_meitong_cost": cost, "ml_close_audit": close}
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"status": "error", "exc": type(e).__name__, "msg": str(e),
                    "traceback": traceback.format_exc()[:1500]},
        ) from e


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


def _monthly_detail_financial_issues(detail: dict, response) -> list[str]:
    """Return missing fields that make an order unsafe for monthly finance."""
    issues: list[str] = []
    headers = getattr(response, "headers", {}) or {}
    raw_missing = headers.get("x-content-missing") or headers.get("X-Content-Missing") or ""
    missing_header = {
        part.strip().lower()
        for part in str(raw_missing).replace(";", ",").split(",")
        if part.strip()
    }
    required_header_fields = {
        "order_items", "payments", "shipping", "date_created", "currency_id",
        "status", "paid_amount", "total_amount",
    }
    issues.extend(f"header:{name}" for name in sorted(missing_header & required_header_fields))

    def _valid_amount(value, *, positive: bool = False) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(number):
            return False
        return number > 0 if positive else number >= 0

    for name in ("id", "date_created", "status"):
        if detail.get(name) in (None, ""):
            issues.append(name)
    if detail.get("status") == "cancelled":
        return sorted(set(issues))

    order_items = detail.get("order_items")
    if not isinstance(order_items, list) or not order_items:
        issues.append("order_items")
    else:
        for index, item in enumerate(order_items):
            item_data = item.get("item") if isinstance(item, dict) else None
            if not isinstance(item_data, dict) or not item_data.get("id"):
                issues.append(f"order_items[{index}].item.id")
            try:
                quantity = int(item.get("quantity") or 0) if isinstance(item, dict) else 0
            except (TypeError, ValueError):
                quantity = 0
            if not isinstance(item, dict) or "quantity" not in item or quantity <= 0:
                issues.append(f"order_items[{index}].quantity")
            if (
                not isinstance(item, dict)
                or "sale_fee" not in item
                or not _valid_amount(item.get("sale_fee"))
            ):
                issues.append(f"order_items[{index}].sale_fee")
            global_price = (item_data or {}).get("global_price") or {}
            if global_price:
                if not _valid_amount(global_price.get("amount")) or not global_price.get("currency"):
                    issues.append(f"order_items[{index}].global_price")
            elif (
                not isinstance(item, dict)
                or not _valid_amount(item.get("unit_price"))
                or not (item.get("currency_id") or detail.get("currency_id"))
            ):
                issues.append(f"order_items[{index}].unit_price_currency")

    if not isinstance(detail.get("payments"), list):
        issues.append("payments")
    shipping = detail.get("shipping")
    if not isinstance(shipping, dict) or not shipping.get("id"):
        issues.append("shipping.id")
    for name in ("paid_amount", "total_amount"):
        if name not in detail or not _valid_amount(detail.get(name)):
            issues.append(name)
    return sorted(set(issues))


async def _report_sku_recent_impl(seller_id: int, recent_n: int, parent_user_id: int,
                                  date_from: str | None = None, date_to: str | None = None,
                                  max_detail_fetch: int | None = None,
                                  refresh_after: int | None = None,
                                  scope_month: str | None = None,
                                  scope_timezone: str | None = None):
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
        windowed = bool(date_from and date_to)
        packs: list[dict] = []
        platform_total: int | None = None
        offset = 0
        while True:
            if windowed:
                page_size = 50
            else:
                if len(packs) >= recent_n:
                    break
                page_size = min(50, recent_n - len(packs))
            params = {"seller": seller_id, "limit": page_size, "offset": offset, "sort": "date_desc"}
            if windowed:
                # The two endpoints use different parameter names. Mixing them is
                # silently accepted but makes the API return an unbounded history.
                prefix = "" if is_cbt else "order."
                params[f"{prefix}date_created.from"] = date_from
                params[f"{prefix}date_created.to"] = date_to
            r = await _ml_get(client, orders_search_url, headers, params)
            if r.status_code == 429:
                await _asyncio.sleep(30)
                r = await _ml_get(client, orders_search_url, headers, params)
            if r.status_code != 200:
                raise HTTPException(502, f"orders/search failed after retry status={r.status_code} body={r.text[:300]}")
            payload = r.json()
            if windowed:
                reported_total = (payload.get("paging") or {}).get("total")
                if not isinstance(reported_total, int) or reported_total < 0:
                    raise HTTPException(502, "orders/search monthly response missing paging.total")
                if platform_total is None:
                    platform_total = reported_total
                elif platform_total != reported_total:
                    raise HTTPException(502, "orders/search paging.total changed during pagination")
            rr = payload.get("results", [])
            if not rr:
                if windowed and len(packs) != platform_total:
                    raise HTTPException(
                        502,
                        f"orders/search pagination truncated expected={platform_total} actual={len(packs)}",
                    )
                break
            packs.extend(rr)
            if windowed:
                if len(packs) == platform_total:
                    break
                if len(packs) > platform_total or len(rr) < page_size:
                    raise HTTPException(
                        502,
                        f"orders/search pagination mismatch expected={platform_total} actual={len(packs)}",
                    )
            elif len(rr) < page_size:
                break
            offset += page_size

        # 2. fetch detail for each inner order — with SQLite cache (Phase 1·④)
        order_details: list[dict] = []
        skipped_429 = 0
        skipped_other = 0
        cache_hits = 0
        new_fetches = 0
        capped = False
        detail_failures: list[dict] = []
        partial_details: list[dict] = []
        for pack in packs:
            if capped:
                break
            # CBT packs have inner `orders[]`; local search may return flat orders directly
            inner = pack.get("orders") or [pack]
            for sub in inner:
                order_id = sub.get("id")
                if not order_id:
                    continue
                # Month-close facts can still change after first payment
                # (cancellation/refund/status). A stable refresh_after cutoff lets
                # repeated capped calls refresh every scoped order exactly once.
                cached = await db.cache_get_order(int(order_id))
                cached_is_fresh = (
                    cached
                    and cached.get("_payload")
                    and (
                        refresh_after is None
                        or int(cached.get("fetched_at") or 0) >= refresh_after
                    )
                )
                if cached_is_fresh:
                    order_details.append(cached["_payload"])
                    cache_hits += 1
                    continue
                # Bound NEW detail fetches per call (paced backfill of a large past month).
                # Cache-hits are free, so repeated calls converge as the cache warms.
                if max_detail_fetch is not None and new_fetches >= max_detail_fetch:
                    capped = True
                    break
                rd = await _ml_get(client, f"{orders_detail_prefix}/{order_id}", headers)
                new_fetches += 1
                if 200 <= rd.status_code < 300:
                    detail = rd.json()
                    issues = _monthly_detail_financial_issues(detail, rd) if windowed else []
                    if issues:
                        skipped_other += 1
                        detail_failures.append(
                            {"order_id": int(order_id), "status": rd.status_code, "issues": issues}
                        )
                    else:
                        order_details.append(detail)
                        await db.cache_put_order(int(order_id), seller_id, detail)
                        if rd.status_code == 206:
                            headers = getattr(rd, "headers", {}) or {}
                            partial_details.append(
                                {
                                    "order_id": int(order_id),
                                    "content_missing": (
                                        headers.get("x-content-missing")
                                        or headers.get("X-Content-Missing")
                                        or ""
                                    ),
                                }
                            )
                elif rd.status_code == 429:
                    skipped_429 += 1
                    detail_failures.append({"order_id": int(order_id), "status": 429})
                else:
                    skipped_other += 1
                    detail_failures.append({"order_id": int(order_id), "status": rd.status_code})

    month_scope_order_ids: list[int] | None = None
    month_scope_unclosed_excluded = 0
    scoped_order_details = order_details
    if scope_month:
        if not scope_timezone:
            raise HTTPException(500, "scope_timezone is required with scope_month")
        try:
            site_timezone = ZoneInfo(scope_timezone)
        except Exception as exc:
            raise HTTPException(500, f"invalid scope_timezone={scope_timezone}") from exc
        month_scope_order_ids = []
        scoped_order_details = []
        for detail in order_details:
            if detail.get("status") == "cancelled":
                continue
            raw_closed = detail.get("date_closed")
            if not raw_closed:
                if detail.get("status") in {"paid", "partially_refunded"}:
                    raise HTTPException(
                        502,
                        f"monthly paid order missing date_closed order_id={detail.get('id')}",
                    )
                month_scope_unclosed_excluded += 1
                continue
            try:
                closed_at = datetime.fromisoformat(str(raw_closed).replace("Z", "+00:00"))
                if closed_at.tzinfo is None:
                    raise ValueError("date_closed has no timezone")
                local_month = closed_at.astimezone(site_timezone).strftime("%Y-%m")
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    502,
                    f"monthly order has invalid date_closed order_id={detail.get('id')}",
                ) from exc
            if local_month == scope_month:
                month_scope_order_ids.append(int(detail["id"]))
                scoped_order_details.append(detail)

    # 3. aggregate by seller_sku
    by_sku: dict[str, dict] = {}
    for od in scoped_order_details:
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
    platform_order_ids = sorted({
        int(sub["id"])
        for pack in packs
        for sub in (pack.get("orders") or [pack])
        if sub.get("id") is not None
    })
    cached_month_unique = None
    if windowed:
        cached_month_rows = await db.cache_list_orders_for_scope(seller_id, date_from[:7])
        cached_month_unique = len({int(item["order_id"]) for item in cached_month_rows})
    return {
        "seller_id": seller_id,
        "recent_n_requested": recent_n,
        "packs_returned": len(packs),
        "platform_total": platform_total,
        "orders_with_detail": len(order_details),
        "cached_month_unique": cached_month_unique,
        "cache_hits": cache_hits,
        "new_fetches": new_fetches,
        "capped": capped,
        "skipped_429": skipped_429,
        "skipped_other": skipped_other,
        "detail_failures": detail_failures,
        "partial_details": partial_details,
        "unique_skus": len(rows),
        "rows": rows,
        "_platform_order_ids": platform_order_ids,
        "_month_scope_order_ids": month_scope_order_ids,
        "month_scope_total": None if month_scope_order_ids is None else len(month_scope_order_ids),
        "month_scope_unclosed_excluded": month_scope_unclosed_excluded,
        "_note": "Token bucket: 80/min search, 1200/min detail. Order details cached in SQLite. Uses global_price (listing USD).",
    }


# ---------- 采购计划: 美客多库存 + 30d/14d 销量 (按 ERP 仓库组聚合) ----------
# seller → {取数用 token_uid, 是否走 CBT(/marketplace 前缀 + 父 token + api-version:2)}
_ML_PROC_SELLER = {
    1407362838: {"token_uid": 1407362838, "cbt": False},   # 本土 FUNLABDIRECTMX
    1436420028: {"token_uid": 1436420028, "cbt": False},   # 本土 FUNLAB_MX
    1502236229: {"token_uid": 1502520822, "cbt": True},    # CBT-FULL (走 CBT 父 token 1502520822)
    2378517428: {"token_uid": 2378517428, "cbt": False},   # 巴西本土
}
# ERP 仓库组 (梁俊辉 2026-05-27 确认): 墨西哥 = 本土MX×2 + CBT-FULL → wid4928; 巴西 → wid12357
# CBT-自发货 1510203792 国内直发不预备货 → 排除
_ML_PROC_GROUPS = {
    "美客多-墨西哥": {"wid": 4928, "sellers": [1407362838, 1436420028, 1502236229]},
    "美客多-巴西":   {"wid": 12357, "sellers": [2378517428]},
}


async def _ml_sku_available(client, headers, seller_id: int, sku: str, is_cbt: bool):
    """该 seller 下 sku 的 available_quantity(匹配 items 求和, 含变体)。?seller_sku= 过滤(实测有效)。"""
    base = "https://api.mercadolibre.com"
    search = (f"{base}/marketplace/users/{seller_id}/items/search" if is_cbt
              else f"{base}/users/{seller_id}/items/search")
    r = await _ml_get(client, search, headers, {"seller_sku": sku, "limit": 20})
    if r.status_code != 200:
        return 0, f"search_{r.status_code}"
    ids = (r.json() or {}).get("results") or []
    avail = 0
    for iid in ids:
        idet = (f"{base}/marketplace/items/{iid}" if is_cbt else f"{base}/items/{iid}")
        hdr = {**headers, "api-version": "2"} if is_cbt else headers
        rd = await _ml_get(client, idet, hdr, {"attributes": "available_quantity,variations,status"})
        if rd.status_code != 200:
            continue
        it = rd.json()
        a = it.get("available_quantity")
        if not a:  # None 或 0 → 用变体求和
            a = sum(int(v.get("available_quantity") or 0) for v in (it.get("variations") or []))
        avail += int(a or 0)
    return avail, None


@app.get("/procurement/ml-stock", dependencies=[Depends(require_service_token)])
async def procurement_ml_stock(skus: str):
    """采购计划用: 给定 ERP SKU 列表(逗号分隔), 返回美客多各 ERP 仓库组的
    available_quantity + 30d/14d 销量(订单缓存)。CBT-自发货已排除。
    返回 {as_of, groups: {组名: {wid, skus: {sku: {available, sales_30d, sales_14d}}}}}。"""
    import datetime, traceback
    sku_list = [s.strip() for s in skus.split(",") if s.strip()]
    today = datetime.date.today()
    since30 = (today - datetime.timedelta(days=30)).isoformat()
    since14 = (today - datetime.timedelta(days=14)).isoformat()
    try:
        out = {}
        tok_cache: dict[int, dict | None] = {}
        async def hdr_for(token_uid: int):
            if token_uid not in tok_cache:
                row = await db.get_token(token_uid)
                tok_cache[token_uid] = {"Authorization": f"Bearer {row['access_token']}"} if row else None
            return tok_cache[token_uid]
        async with httpx.AsyncClient(timeout=180) as client:
            for gname, g in _ML_PROC_GROUPS.items():
                grp = {"wid": g["wid"], "skus": {s: {"available": 0, "sales_30d": 0, "sales_14d": 0} for s in sku_list}}
                for seller_id in g["sellers"]:
                    cfg = _ML_PROC_SELLER[seller_id]
                    headers = await hdr_for(cfg["token_uid"])
                    if not headers:
                        continue
                    # 销量: 一次拉该 seller 近30天订单缓存, 按 SKU+日期窗聚合 units
                    for od in await db.cache_list_orders_since(seller_id, since30):
                        dc = (od.get("date_created") or "")[:10]
                        for item in ((od.get("_payload") or {}).get("order_items") or []):
                            it = item.get("item") or {}
                            isku = it.get("seller_sku") or it.get("seller_custom_field") or ""
                            if isku in grp["skus"]:
                                q = int(item.get("quantity") or 0)
                                grp["skus"][isku]["sales_30d"] += q
                                if dc >= since14:
                                    grp["skus"][isku]["sales_14d"] += q
                    # 库存: 每 SKU 1 搜 + 命中 items detail (便宜, CBT 负载小)
                    for sku in sku_list:
                        av, _err = await _ml_sku_available(client, headers, seller_id, sku, cfg["cbt"])
                        grp["skus"][sku]["available"] += av
                out[gname] = grp
        return {"as_of": today.isoformat(), "groups": out}
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()}


@app.post("/report/sync-meitong-cost", dependencies=[Depends(require_service_token)])
async def sync_meitong_cost(period: str, months: int = 12, commit: bool = False):
    """美通中转 头程/海外仓成本 → ML报表两列(方案A: 只灌经美通中转SKU, 其余留空)。
    源: 美通订单API(头程=收费重×费率快照) + 指令明细(海外仓=换标箱数×单箱费快照), 不碰美通账单。
    period 如 month_2026-04; months=单价滚动窗口(默认12); commit=False 只预览不写。
    月度 cron 应在 sync-feishu-monthly 之后调(当月行先生成)。sync 同步(urllib), FastAPI 自动 threadpool。"""
    import traceback
    from app import meitong_cost, ml_close
    try:
        if commit:
            await ml_close.invalidate_ab_verification(period, "meitong_cost_sync")
        result = await run_in_threadpool(meitong_cost.run, period, months, commit)
        if result.get("status") == "error":
            raise HTTPException(status_code=502, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"status": "error", "exc": type(e).__name__, "msg": str(e),
                    "traceback": traceback.format_exc()[:1500]},
        ) from e


@app.post("/report/ml-close/audit", dependencies=[Depends(require_service_token)])
async def ml_close_audit(month: str | None = None, period: str | None = None,
                         commit: bool = False, run_cost_preview: bool = True,
                         ab_verified: bool | None = None):
    """Audit ML monthly close state and optionally upsert the close status table."""
    import traceback
    from app import ml_close
    try:
        return await ml_close.audit(
            month=month,
            period=period,
            commit=commit,
            run_cost_preview=run_cost_preview,
            ab_verified=ab_verified,
        )
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()[:3000]}


@app.post("/report/ml-close/recalc-cost", dependencies=[Depends(require_service_token)])
async def ml_close_recalc_cost(month: str | None = None, period: str | None = None,
                               commit: bool = True, ab_verified: bool | None = None):
    """Recalculate Meitong/Mokeduo/Sanmu cost, then run close audit."""
    import traceback
    from app import ml_close
    try:
        return await ml_close.recalc_cost(
            month=month,
            period=period,
            commit=commit,
            ab_verified=ab_verified,
        )
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()[:3000]}


@app.api_route("/report/ml-close/card", methods=["GET", "POST"], dependencies=[Depends(require_service_token)])
async def ml_close_card(kind: str | None = None, month: str | None = None, period: str | None = None,
                        send: bool = False, receive_id: str | None = None,
                        receive_id_type: str = "chat_id"):
    """Build, and optionally send, the next ML monthly close interactive card."""
    import traceback
    from app import ml_close
    try:
        return await ml_close.card_endpoint(kind=kind, month=month, period=period, send=send,
                                            receive_id=receive_id, receive_id_type=receive_id_type)
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()[:3000]}


@app.api_route("/report/ml-close/status", methods=["GET", "POST"], dependencies=[Depends(require_service_token)])
async def ml_close_status(month: str | None = None, period: str | None = None):
    """Read ML monthly close status for downstream gates."""
    import traceback
    from app import ml_close
    try:
        return await ml_close.status_endpoint(month=month, period=period)
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()[:3000]}


@app.post("/report/ml-close/confirm", dependencies=[Depends(require_service_token)])
async def ml_close_confirm(req: Request, action: str | None = None, month: str | None = None,
                           period: str | None = None, operator_id: str | None = None,
                           operator_name: str | None = None, message_id: str | None = None,
                           patch_message: bool = False):
    """Handle Feishu interactive-card callbacks for the ML monthly close loop."""
    import traceback
    from app import ml_close
    body = {}
    try:
        body = await req.json()
    except Exception:
        body = {}
    payload = dict(body or {})
    for k, v in {
        "action": action,
        "month": month,
        "period": period,
        "operator_id": operator_id,
        "operator_name": operator_name,
        "message_id": message_id,
        "patch_message": patch_message,
    }.items():
        if v not in (None, False):
            payload[k] = v
    try:
        return await ml_close.confirm_action(payload)
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()[:3000]}


@app.post("/report/ml-unified-monthly", dependencies=[Depends(require_service_token)])
async def ml_unified_monthly(month: str | None = None, period: str | None = None,
                             commit: bool = False):
    """Preview or replay the finance-approved 47-column ML monthly report.

    Preview is read-only. Direct commit is allowed only for an already finance-confirmed
    month; the finance button uses the generator internally before writing that final state.
    """
    from app import ml_close, unified_report

    normalized_period, _ = ml_close.normalize_period(month, period)
    approved_report_hash = ""
    if commit:
        # Recompute the report hash from the live Base.  The status row alone
        # may still describe an older, approved version after a manual edit.
        close_status = await ml_close.audit(
            period=normalized_period,
            commit=False,
            run_cost_preview=False,
        )
        if (
            close_status.get("state") != "财务已确认终稿"
            or close_status.get("ab_verified") is not True
        ):
            raise HTTPException(
                409,
                f"{normalized_period} 尚未完成当前报表版本的 A/B 与财务终稿确认；只允许 commit=false 预览。",
            )
        approved_report_hash = str(close_status.get("report_hash") or "")
    try:
        report = await unified_report.generate(
            normalized_period,
            commit=commit,
            expected_source_hash=approved_report_hash or None,
        )
        if commit:
            live_after = await ml_close.audit(
                period=normalized_period,
                commit=False,
                run_cost_preview=False,
            )
            if (
                live_after.get("ab_verified") is not True
                or str(live_after.get("report_hash") or "") != approved_report_hash
            ):
                raise HTTPException(
                    409,
                    f"{normalized_period} 生成期间生产表已变化；当前版本需重新完成 A/B 对账。",
                )
        return report
    except unified_report.ProductMappingError as exc:
        raise HTTPException(
            409,
            detail={
                "error": "ERP_PRODUCT_MAPPING_FAILED",
                "message": str(exc),
                "issues": exc.issues,
            },
        ) from exc
    except unified_report.ReportGenerationInProgressError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except unified_report.ReportGenerationError as exc:
        raise HTTPException(502, detail=f"月报生成失败：{exc}") from exc


@app.post("/report/meitong-diag", dependencies=[Depends(require_service_token)])
def meitong_diag(period: str = "month_2026-05", months: int = 12):
    import traceback
    from app import meitong_cost
    try:
        return meitong_cost.diag(period, months)
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()}


@app.post("/report/validate-skus", dependencies=[Depends(require_service_token)])
def validate_skus(skus: str, period: str = "month_2026-05"):
    """校验逗号分隔的 ERP SKU 是否存在于领星产品库 + 是否在 period 的 ML 报表行(有销量)。"""
    import traceback
    from app import meitong_cost as mc
    try:
        name2erp, sku_set = mc.load_erp()
        recs, pt = [], None
        while True:
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{mc.ML_APP}/tables/{mc.ML_T}/records/search?page_size=500" + (f"&page_token={pt}" if pt else "")
            d = mc._fs(url, {}, "POST").get("data", {})
            recs += d.get("items", []); pt = d.get("page_token")
            if not d.get("has_more") or not pt:
                break
        ml_skus = set(mc._txt(r["fields"].get("SKU")) for r in recs if mc._txt(r["fields"].get("周期")) == period)
        out = {}
        for s in [x.strip() for x in skus.split(",") if x.strip()]:
            out[s] = {"exists_lingxing": s in sku_set, "in_ml_period": s in ml_skus}
        return {"period": period, "lingxing_total": len(sku_set), "result": out}
    except Exception as e:
        return {"status": "error", "exc": type(e).__name__, "msg": str(e), "traceback": traceback.format_exc()}


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
    parent_row = await db.get_token(parent_user_id)
    if not parent_row:
        raise HTTPException(404, "parent token not found")
    events = await db.claim_pending_events(limit)
    if not events:
        return {"processed": 0, "events": []}

    # Per-app routing: CBT events use the CBT parent token + /marketplace prefix; local-store
    # events (local_mx / local_br apps) use the seller's OWN token + the plain /orders path.
    # Determine CBT-vs-local from the event's application_id → ml_apps.account_type, defaulting
    # to CBT for legacy/unknown events so existing CBT behavior is unchanged. Tokens and app
    # rows are cached per request to avoid re-querying the DB for every event.
    _token_cache: dict[int, dict | None] = {parent_user_id: parent_row}
    _app_cache: dict[str, dict | None] = {}

    async def _token_for(user_id: int) -> dict | None:
        if user_id not in _token_cache:
            _token_cache[user_id] = await db.get_token(user_id)
        return _token_cache[user_id]

    async def _app_for(client_id: str) -> dict | None:
        if client_id not in _app_cache:
            _app_cache[client_id] = await db.get_app_by_client_id(client_id) if client_id else None
        return _app_cache[client_id]

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for ev in events:
            topic = ev["topic"] or ""
            resource = ev["resource"] or ""
            ev_user = int(ev.get("user_id") or 0)
            app_row = await _app_for(str(ev.get("application_id") or ""))
            # default CBT for legacy/unknown events (preserves original behavior)
            is_cbt = (app_row.get("account_type") == "cbt") if app_row else True
            tok_row = parent_row if is_cbt else (await _token_for(ev_user) or parent_row)
            if not tok_row:
                await db.mark_event_failed(ev["id"], "no token for event")
                results.append({"id": ev["id"], "ok": False, "error": "no token", "resource": resource})
                continue
            headers = {"Authorization": f"Bearer {tok_row['access_token']}"}
            # Decide URL + cache target by resource path (more robust than topic name).
            # resource path is canonical (e.g. /orders/<id>, /items/<id>, /questions/<id>)
            is_order = resource.startswith("/orders/") or "orders" in topic
            is_item = resource.startswith("/items/") or topic == "items"
            url = f"https://api.mercadolibre.com{resource}"
            # CBT order detail needs the /marketplace prefix; local orders use the plain path.
            if is_order and is_cbt and "/marketplace/" not in url:
                url = f"https://api.mercadolibre.com/marketplace{resource}"
            try:
                r = await _ml_get(client, url, headers)
                if r.status_code == 200:
                    detail = r.json()
                    if is_order:
                        seller_id = ((detail.get("seller") or {}).get("id")
                                     or (detail.get("orders") or [{}])[0].get("seller", {}).get("id")
                                     or ev_user or 0)
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
    """Backfill via ML missed_feeds for EVERY registered app (CBT + local_mx + local_br + ...),
    not just CBT. Each app's missed feed is queried with that app's client_id plus any token
    authorized under it. One app erroring (e.g. 429) does not block the others.

    Run daily via n8n cron.
    """
    apps = await db.list_apps()
    tokens = await db.list_tokens()
    # app_key → first token (access_token) authorized under that app
    token_by_appkey: dict[str, dict] = {}
    for t in tokens:
        ak = t.get("app_key")
        if ak and ak not in token_by_appkey and t.get("access_token"):
            token_by_appkey[ak] = t

    # Build (client_id, access_token, app_key) jobs, deduped by client_id.
    jobs: list[tuple[str, str, str]] = []
    seen_client_ids: set[str] = set()
    for a in apps:
        cid = str(a.get("client_id") or "")
        ak = a.get("app_key") or ""
        if not cid or cid in seen_client_ids:
            continue
        tok = token_by_appkey.get(ak)
        if not tok:
            continue  # no token authorized this app yet → nothing to query
        jobs.append((cid, tok["access_token"], ak))
        seen_client_ids.add(cid)

    # Legacy CBT fallback: guarantee CBT is covered even if its token row has no app_key link.
    legacy_cid = os.getenv("ML_APP_ID")
    if legacy_cid and legacy_cid not in seen_client_ids:
        prow = await db.get_token(parent_user_id)
        if prow:
            jobs.append((legacy_cid, prow["access_token"], "cbt(legacy)"))
            seen_client_ids.add(legacy_cid)

    by_app: list[dict] = []
    total_missed = 0
    total_enqueued = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for cid, access_token, ak in jobs:
            headers = {"Authorization": f"Bearer {access_token}"}
            try:
                r = await _ml_get(client, f"https://api.mercadolibre.com/missed_feeds?app_id={cid}", headers)
            except Exception as e:
                by_app.append({"app_key": ak, "client_id": cid, "error": f"{type(e).__name__}: {str(e)[:120]}"})
                continue
            if r.status_code != 200:
                by_app.append({"app_key": ak, "client_id": cid, "http": r.status_code, "body": r.text[:160]})
                continue
            payload = r.json()
            missed = payload if isinstance(payload, list) else (payload.get("missed_feeds") or [])
            enq = 0
            for n in missed:
                if await db.enqueue_event(n):
                    enq += 1
            total_missed += len(missed)
            total_enqueued += enq
            by_app.append({"app_key": ak, "client_id": cid, "missed": len(missed), "enqueued": enq})
    return {"apps_checked": len(jobs), "missed_total": total_missed,
            "newly_enqueued": total_enqueued, "by_app": by_app}


# ---------- M3 → Feishu Bitable writer ----------

# Bitable target (created 2026-05-12)
FEISHU_BASE_APP_TOKEN = os.getenv("FEISHU_BASE_APP_TOKEN", "WM3LbBr76aRqMys2of8c1dGInEb")
FEISHU_BASE_TABLE_ID = os.getenv("FEISHU_BASE_TABLE_ID", "tbl09sRPkX35PDfU")

# Map child seller_id → store option label (must match Bitable single-select option)
SHOP_LABEL: dict[int, str] = {
    # 1510203792 (CBT-自发货) 已剔除 (2026-06-16): 幽灵店, marketplace/orders/search?seller=1510203792
    # 返回的订单内层 seller 全是 1502236229(CBT-FULL) → 双重计数 CBT-FULL (~$1,438/月). 俊辉确认该店无真实订单.
    1502236229: "ML CBT-FULL (1502236229)",
    1407362838: "ML 本土1店 FUNLABDIRECTMX",
    1436420028: "ML 本土2店 FUNLAB_MX",
    2378517428: "ML 巴西本土店 AIRSOFT COMERCIAL",
    3383185411: "ML 本土3店 DISTRIBUIDOR VALMIGOZ",
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
        s = date_str[:10]
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return None


_LOCAL_SELLER_TIMEZONES = {
    2378517428: "America/Sao_Paulo",
    3383185411: "America/Mexico_City",
}


def _site_local_closed_date(seller_id: int, order: dict) -> str:
    """Return the seller-site civil date when the sale was confirmed."""
    timezone_name = _LOCAL_SELLER_TIMEZONES.get(seller_id)
    if not timezone_name:
        raise HTTPException(500, f"monthly timezone missing seller_id={seller_id}")
    raw_closed = order.get("date_closed")
    if not raw_closed:
        raise HTTPException(502, f"monthly order missing date_closed order_id={order.get('id')}")
    try:
        closed_at = datetime.fromisoformat(str(raw_closed).replace("Z", "+00:00"))
        if closed_at.tzinfo is None:
            raise ValueError("date_closed has no timezone")
        return closed_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            502,
            f"monthly order has invalid date_closed order_id={order.get('id')}",
        ) from exc


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

    if period.startswith("month_"):
        from app import ml_close
        await ml_close.invalidate_ab_verification(period, "recent_sync_to_month_period")

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
async def report_sync_feishu_monthly(seller_id: int, month: str, background_tasks: BackgroundTasks,
                                     period_label: str = "", nowait: bool = False,
                                     commit: bool = True,
                                     preserve_existing_as: str = ""):
    """Dispatcher. nowait=true → schedule aggregation in background, return 202 immediately
    (avoids Zeabur gateway ~150s connection reset on heavy sellers like CBT-FULL 1502236229,
    which made the monthly cron 9ZvARULB0wIp19yp false-alarm even though data lands fine).
    Default nowait=false → synchronous; behavior unchanged for every existing caller."""
    if seller_id not in SHOP_LABEL:
        raise HTTPException(400, f"unknown seller_id {seller_id}; allowed: {list(SHOP_LABEL.keys())}")
    if nowait:
        background_tasks.add_task(
            _sync_feishu_monthly_impl,
            seller_id,
            month,
            period_label,
            commit,
            preserve_existing_as,
        )
        return {"status": "accepted", "mode": "background", "seller_id": seller_id, "month": month,
                "commit": commit,
                "note": "Aggregation runs in background; verify via Feishu 数据拉取时间 in ~3-5min."}
    result = await _sync_feishu_monthly_impl(
        seller_id, month, period_label, commit, preserve_existing_as
    )
    if commit and result.get("status") != "synced":
        raise HTTPException(
            422,
            f"monthly sync blocked seller={seller_id} month={month} status={result.get('status')}",
        )
    return result


async def _sync_feishu_monthly_impl(seller_id: int, month: str, period_label: str = "",
                                    commit: bool = True,
                                    preserve_existing_as: str = ""):
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
    # 🚨 CBT-FULL(1502236229) 不走主sync(2026-06-17 task3): orders/search 被429墙→缓存仅~85%不全,
    # 且 Full仓储无CBT API → 主sync CBT毛利结构性不准. CBT 改走官方导出解析(cbt_export_ingest.py,
    # B口径完整准确) 按SKU update 写飞书. 主sync只写本土店(缓存完整). 防主sync用85%数字覆盖导出行.
    if seller_id == 1502236229:
        return {"status": "skipped_cbt", "seller_id": seller_id, "month": month,
                "note": "CBT-FULL 走官方导出解析(cbt_export_ingest.py), 不走主sync(缓存仅85%不全)."}
    period = period_label or f"month_{month}"
    if preserve_existing_as and (
        preserve_existing_as == period
        or not re.fullmatch(r"month_\d{4}-\d{2}_[A-Za-z0-9_-]+", preserve_existing_as)
    ):
        raise HTTPException(
            400,
            "preserve_existing_as 格式不安全，示例：month_2026-08_original_20260908",
        )
    sync_started_at = time.time_ns()

    cached_rows = await db.cache_list_orders_for_scope(seller_id, month)
    if not cached_rows:
        return {"status": "no_cache", "seller_id": seller_id, "month": month,
                "hint": "Run /admin/backfill-orders to fill cache, or wait for webhook to populate."}

    # Aggregate by SKU — Phase B2: also extract shipment_id + refund amount per order
    by_sku: dict[str, dict] = {}
    item_id_to_sku: dict[str, str] = {}
    # Order-level shipment + refund data for later allocation
    order_shipments: list[tuple[int, int, int]] = []  # (shipment_id, seller_id, order_id)
    order_to_sku: dict[int, list[tuple[str, int]]] = {}  # order_id → [(sku, units), ...]
    refunds_by_order: dict[int, float] = {}  # order_id → total refunded amount in order currency
    cancelled_count = 0
    for cr in cached_rows:
        od = cr.get("_payload") or {}
        order_id = int(od.get("id") or 0)
        # Phase B2.1 fix: skip cancelled orders entirely. They have paid_amount=0
        # but order_items still show quantity > 0 — including them inflates revenue
        # and double-counts with the refund field. The cancelled order's
        # transaction_amount_refunded is also in buyer currency (MXN for CBT),
        # not seller currency, so it can't be reliably summed.
        if od.get("status") == "cancelled":
            cancelled_count += 1
            continue
        order_items_skus: list[tuple[str, int]] = []
        # P2.5 (2026-05-21): order-level buyer→seller currency ratio.
        # CBT: paid_amount(USD 卖家币) vs total_amount(MXN 买家币) → ratio ≈ 1/17
        # 本土店: paid_amount == total_amount (同币种) → ratio = 1.0
        # 用 ratio 把 discounts.amounts.seller (MXN 买家币) 换回订单卖家币
        o_paid = float(od.get("paid_amount") or 0)
        o_total = float(od.get("total_amount") or 0)
        buyer_to_seller_ratio = 1.0
        if o_total > 0 and o_paid > 0 and abs(o_paid - o_total) / o_total > 0.05:
            buyer_to_seller_ratio = o_paid / o_total
        for item in (od.get("order_items") or []):
            it = item.get("item") or {}
            sku = it.get("seller_sku") or it.get("seller_custom_field") or "(no_sku)"
            ml_item_id = it.get("id")
            if ml_item_id and sku and sku != "(no_sku)":
                item_id_to_sku[ml_item_id] = sku
            gp = it.get("global_price") or {}
            if gp:
                currency = gp.get("currency") or "?"
                amount = float(gp.get("amount") or 0)
            else:
                currency = item.get("currency_id") or od.get("currency_id") or "?"
                amount = float(item.get("unit_price") or 0)
            quantity = int(item.get("quantity") or 1)
            sale_fee = float(item.get("sale_fee") or 0)
            # P2.5: seller-funded discount per item. amounts.seller 是买家币种,
            # 用 order-level ratio 换成卖家币种 (CBT 必换/本土 ratio=1 无影响)
            seller_discount_buyer_cur = sum(
                float((d.get("amounts") or {}).get("seller") or 0)
                for d in (item.get("discounts") or [])
            )
            seller_discount_local = seller_discount_buyer_cur * buyer_to_seller_ratio
            cell = by_sku.setdefault(sku, {
                "sku": sku, "orders_count": 0, "units": 0, "revenue_total": 0.0,
                "commission_total": 0.0, "shipping_total": 0.0, "refund_total": 0.0, "refund_units": 0,
                "discount_total": 0.0,
                "currency": currency, "sample_title": it.get("title"),
                "first_seen": None, "last_seen": None,
            })
            cell["orders_count"] += 1
            cell["units"] += quantity
            cell["revenue_total"] += amount * quantity
            # 🚨 sale_fee 语义按端点相反(2026-06-17 实测):
            #   CBT /marketplace(有global_price): sale_fee=PER-LINE总额(order 2000016704287954 qty=2/
            #     unit_price=4.8/sale_fee=0.72=整行2单佣金=导出L) → 不×qty.
            #   本土店原生 /orders(无global_price): sale_fee=PER-UNIT(qty=2单 sale_fee=100.69=1单16%) →
            #     必须×qty, 否则多件单漏算佣金(本土店实测漏100.69=billing 2189.95-API 2089.26).
            cell["commission_total"] += sale_fee if gp else sale_fee * quantity
            cell["discount_total"] = cell.get("discount_total", 0) + seller_discount_local
            order_items_skus.append((sku, quantity))
            dc = _site_local_closed_date(seller_id, od)
            if dc:
                cell["first_seen"] = min(cell["first_seen"] or dc, dc)
                cell["last_seen"] = max(cell["last_seen"] or dc, dc)
        # Shipment id (for shipping cost lookup)
        ship = od.get("shipping") or {}
        sid = ship.get("id")
        if sid and order_id:
            order_shipments.append((int(sid), seller_id, order_id))
            order_to_sku[order_id] = order_items_skus
        # Refund total (sum across payments)
        refunded = 0.0
        for p in (od.get("payments") or []):
            refunded += float(p.get("transaction_amount_refunded") or 0)
        if refunded > 0 and order_id:
            refunds_by_order[order_id] = refunded
            # P2.4 (2026-05-18, verified 9 samples): cancelled orders are skipped
            # above (line ~1097), so any refund reaching here is a NON-cancelled
            # partial refund. For CBT, transaction_amount_refunded is buyer currency
            # (MXN), NOT seller USD — biggest deduction risk ~17x. Observed count is
            # currently 0; warn loudly so we catch the first one and add FX conversion
            # before trusting refund_total. (Frankie decision: keep formula as-is + warn)
            print(f"[WARN] P2.4 non-cancelled refund: order={order_id} seller={seller_id} "
                  f"status={od.get('status')} refunded={refunded} order_cur={od.get('currency_id')} "
                  f"— verify currency (CBT refunded=MXN buyer ccy) before trusting refund_total")

    rows = sorted(by_sku.values(), key=lambda x: x["revenue_total"], reverse=True)
    if not rows:
        return {"status": "no_data", "seller_id": seller_id, "month": month,
                "cached_orders": len(cached_rows)}

    # Enrich with Lingxing cost (cg_price) + monthly FX rate → compute RMB revenue/cost/profit
    from app import lingxing, advertising, billing
    try:
        products = await lingxing.fetch_all_products()
        fx_map = await lingxing.fetch_fx_rate(month)
    except Exception as e:
        products, fx_map = {}, {}
        lingxing_error = str(e)[:200]
    else:
        lingxing_error = None

    # Phase B1.3: pull advertising at ITEM-level with full metrics dict
    ad_sku_metrics: dict[str, dict[str, float]] = {}
    ad_unallocated_metrics: dict[str, float] = {}
    ad_advertised_unsold: list[str] = []
    ad_currency = "?"
    ad_error = None
    ad_items_count = 0
    ad_advertiser_id = advertising.ADVERTISER_BY_SELLER.get(seller_id)
    ad_token_user = advertising.TOKEN_USER_FOR_ADVERTISING.get(seller_id, seller_id)
    if ad_advertiser_id:
        try:
            ad_items = await advertising.fetch_ad_items_for_month(ad_advertiser_id, month, ad_token_user, strict=True)
            ad_items_count = len(ad_items)
            ad_sku_metrics, ad_unallocated_metrics, ad_advertised_unsold = await advertising.attribute_ad_metrics_by_item_id(
                ad_items, item_id_to_sku, token_user_id=ad_token_user
            )
            ad_currency = advertising.AD_CURRENCY_BY_ADVERTISER.get(ad_advertiser_id, "?")
        except Exception as e:
            ad_error = type(e).__name__
            print(
                f"[ERROR] advertising fetch failed: seller_id={seller_id} month={month} "
                f"error={type(e).__name__} detail={str(e)[:500]}"
            )
            failure_message = (
                f"广告费抓取失败：{SHOP_LABEL[seller_id]} / {month}。"
                "本次未写入，原报表数据保持不变；禁止将抓取失败自动记为 0。"
            )
            if commit:
                try:
                    from app import ml_close

                    await ml_close.record_advertising_failure(
                        period=period,
                        shop=SHOP_LABEL[seller_id],
                        message=failure_message,
                    )
                except Exception as notify_error:
                    print(
                        f"[ERROR] failed to publish advertising fetch failure: "
                        f"seller_id={seller_id} month={month} error={type(notify_error).__name__}"
                    )
            raise HTTPException(
                502,
                detail=failure_message,
            ) from e
    ad_sku_cost: dict[str, float] = {k: v.get("cost", 0.0) for k, v in ad_sku_metrics.items()}
    ad_unallocated_cost = ad_unallocated_metrics.get("cost", 0.0)

    # Billing-period exports contain several non-order charges that are absent
    # from the orders and Product Ads occurrence APIs. Pull them separately and
    # fail closed: treating a temporary billing failure as zero overstates profit.
    try:
        billing_adjustments = await billing.fetch_month_adjustments(seller_id, month)
    except Exception as e:
        print(
            f"[ERROR] billing adjustments fetch failed: seller_id={seller_id} month={month} "
            f"error={type(e).__name__} detail={str(e)[:500]}"
        )
        raise HTTPException(
            502,
            detail=(
                f"账单费用抓取失败：{SHOP_LABEL[seller_id]} / {month}。"
                "本次未写入，原报表数据保持不变；禁止将抓取失败自动记为 0。"
            ),
        ) from e
    billing_currency = str(billing_adjustments.get("currency") or rows[0].get("currency") or "?")
    if int(billing_adjustments.get("unclassified_count") or 0):
        raise HTTPException(
            502,
            detail=(
                f"发现未识别账单费用：{SHOP_LABEL[seller_id]} / {month}。"
                "本次未写入，需先确认费用类型和应写字段。"
                f" amount={billing_adjustments.get('unclassified_amount')} "
                f"samples={billing_adjustments.get('unclassified')}"
            ),
        )

    # Phase B1.4: pull shop-level visits (per ML user/items_visits endpoint).
    # CBT sellers return 403 → None. Used to compute 整店 CVR = sum(件数) / 访客.
    shop_visits = await advertising.fetch_shop_visits_for_month(seller_id, month)
    total_units_in_shop = sum(r["units"] for r in rows)
    shop_cvr = (total_units_in_shop / shop_visits) if (shop_visits and shop_visits > 0) else 0

    # Phase B2: fetch shipping costs (cache-first) + allocate per SKU
    # Each shipment.sender_cost is allocated to the order's SKUs by units share.
    shipping_costs_fetched = 0
    shipping_skipped = 0
    if order_shipments:
        from app import shipping
        ship_token_user = shipping.SHIPPING_TOKEN_USER.get(seller_id, seller_id)
        # Wall-clock budget for the live shipment-cost fetch. CBT-FULL has 147+ shipments on a
        # shared, rate-limited parent token; without a bound the fetch can run 20+ min, the
        # Zeabur gateway resets the connection, and the function never reaches the Feishu write
        # (数据拉取时间 stays stale → monthly cron false-alarms). Cached shipments seed instantly;
        # uncached ones fetch up to the budget, the rest fill in on later runs.
        ship_budget = float(os.environ.get("SHIP_FETCH_BUDGET_S", "75"))
        ship_results = await shipping.fetch_many_shipping_costs(
            order_shipments, ship_token_user, concurrency=5, budget_s=ship_budget)
        for sid, seller, oid in order_shipments:
            r = ship_results.get(sid)
            sku_units = order_to_sku.get(oid) or []
            try:
                sender_cost = float(r["sender_cost"])
                shipping_currency = str(r["currency"] or "")
            except (TypeError, KeyError, ValueError):
                shipping_skipped += 1
                continue
            expected_currencies = {
                str(by_sku[sku].get("currency") or "")
                for sku, _ in sku_units
                if sku in by_sku
            }
            total_units = sum(u for _, u in sku_units)
            if (
                not math.isfinite(sender_cost)
                or sender_cost < 0
                or not shipping_currency
                or len(expected_currencies) != 1
                or shipping_currency not in expected_currencies
                or total_units <= 0
            ):
                shipping_skipped += 1
                continue
            shipping_costs_fetched += 1
            if sender_cost <= 0:
                continue
            for sku, u in sku_units:
                share = sender_cost * (u / total_units)
                if sku in by_sku:
                    by_sku[sku]["shipping_total"] = by_sku[sku].get("shipping_total", 0) + share

    # Phase B2: allocate refunds per order to SKUs (by revenue share)
    for oid, refund_amt in refunds_by_order.items():
        sku_units = order_to_sku.get(oid) or []
        if not sku_units:
            continue
        # refund is in the order's currency (USD for CBT, MXN for local MX, BRL for BR)
        # Distribute proportional to SKU units in the order
        total_units = sum(u for _, u in sku_units)
        if total_units <= 0:
            continue
        for sku, u in sku_units:
            share = refund_amt * (u / total_units)
            if sku in by_sku:
                by_sku[sku]["refund_total"] = by_sku[sku].get("refund_total", 0) + share
                by_sku[sku]["refund_units"] = by_sku[sku].get("refund_units", 0) + u  # treat fully refunded as fully returned units

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

    import time as _t
    pulled_at_ms = int(_t.time() * 1000)
    records: list[dict] = []
    skus_missing_cost: list[str] = []
    currencies_missing_fx: list[str] = []

    def _positive_finite(value) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    for r in rows:
        rev = r["revenue_total"]; cnt = r["orders_count"]
        currency = r.get("currency") or "?"
        commission_local = r.get("commission_total") or 0
        # Phase B2: shipping (seller cost) + refunds, both in seller currency
        shipping_local = r.get("shipping_total") or 0
        refund_local = r.get("refund_total") or 0
        # P2.5: seller-funded discount (already converted to seller currency in order loop)
        discount_local = r.get("discount_total") or 0
        refund_units = int(r.get("refund_units") or 0)
        refund_rate = (refund_units / r["units"]) if r["units"] else 0
        # Phase B1.3: pull full ad metrics dict for this SKU
        ad_m = ad_sku_metrics.get(r["sku"], {})
        ad_cost_local = ad_m.get("cost", 0.0)
        ad_clicks = int(ad_m.get("clicks", 0))
        ad_prints = int(ad_m.get("prints", 0))
        ad_direct_local = ad_m.get("direct_amount", 0.0)
        ad_total_local = ad_m.get("total_amount", 0.0)
        ad_direct_qty = int(ad_m.get("direct_items_quantity", 0))
        ad_indirect_qty = int(ad_m.get("indirect_items_quantity", 0))
        ad_attributed_qty = ad_direct_qty + ad_indirect_qty
        ctr = (ad_clicks / ad_prints) if ad_prints else 0
        cpc_local = (ad_cost_local / ad_clicks) if ad_clicks else 0
        ml_roas = (ad_total_local / ad_cost_local) if ad_cost_local else 0
        ad_cvr = (ad_attributed_qty / ad_clicks) if ad_clicks else 0
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
            "物流费(原币)": round(shipping_local, 2),
            "退款金额(原币)": round(refund_local, 2),
            "卖家折扣(原币)": round(discount_local, 2),
            "退款率": round(refund_rate, 4),
            "广告展示": ad_prints,
            "广告点击": ad_clicks,
            "CTR": round(ctr, 4),
            "CPC(原币)": round(cpc_local, 4),
            "广告直接销售(原币)": round(ad_direct_local, 2),
            "ML ROAS": round(ml_roas, 2),
            "广告归因件数": ad_attributed_qty,
            "广告CVR": round(ad_cvr, 4),
            "整店访客": int(shop_visits or 0),
            "整店CVR": round(shop_cvr, 4),
            "商品标题": r.get("sample_title") or "",
            "数据拉取时间": pulled_at_ms,
        }
        # Purchase cost is RMB-denominated and does not depend on the sales FX
        # lookup.  Validate it even when the FX source is unavailable, otherwise
        # an empty FX response could bypass the cost guard and replace good rows
        # with incomplete RMB financials.
        erp_sku = lingxing.resolve_erp_sku(r["sku"])
        prod = products.get(erp_sku)
        purchase_cost_rmb: float | None = None
        if prod and prod.get("cg_price") is not None:
            try:
                cgp = _positive_finite(prod["cg_price"])
                if cgp is None:
                    raise ValueError("invalid cg_price")
                purchase_cost_rmb = round(cgp * r["units"], 2)
                fields["采购成本(RMB)"] = purchase_cost_rmb
            except (TypeError, ValueError):
                skus_missing_cost.append(r["sku"])
        else:
            skus_missing_cost.append(r["sku"])
        fx = _positive_finite(fx_map.get(currency))
        if fx is not None:
            fields["我的汇率"] = round(fx, 4)
            rev_rmb = rev * fx
            commission_rmb = commission_local * fx
            ad_cost_rmb = ad_cost_local * fx
            vat_rmb = vat_estimate_local * fx
            ad_direct_rmb = ad_direct_local * fx
            shipping_rmb = shipping_local * fx
            refund_rmb = refund_local * fx
            discount_rmb = discount_local * fx
            fields["营收(RMB)"] = round(rev_rmb, 2)
            fields["ML佣金(RMB)"] = round(commission_rmb, 2)
            fields["广告费(RMB)"] = round(ad_cost_rmb, 2)
            fields["VAT估算(RMB)"] = round(vat_rmb, 2)
            fields["物流费(RMB)"] = round(shipping_rmb, 2)
            fields["退款金额(RMB)"] = round(refund_rmb, 2)
            fields["卖家折扣(RMB)"] = round(discount_rmb, 2)
            # Phase B1.3 业务级指标
            tacos = (ad_cost_rmb / rev_rmb) if rev_rmb else 0
            natural_rmb = rev_rmb - ad_direct_rmb
            natural_ratio = (natural_rmb / rev_rmb) if rev_rmb else 0
            fields["TACOS"] = round(tacos, 4)
            fields["自然销售(RMB)"] = round(natural_rmb, 2)
            fields["自然销售占比"] = round(natural_ratio, 4)
            if purchase_cost_rmb is not None:
                fields["简易毛利(RMB)"] = round(rev_rmb - purchase_cost_rmb, 2)
                # 🚨 全额毛利(RMB) 2026-06-17 task4 改为飞书**公式字段**(不再代码写):
                #   = 营收 - SUM(采购,佣金,广告,VAT,物流,退款,Full仓储,头程,海外仓)
                #   各写入方(主sync/CBT导出解析/美通)只写自己的成本列, 公式自动重算 → 无排序/幂等问题.
                #   折扣绝不进公式(已是折后K净额); 退款/头程/海外仓/Full仓储统一由公式扣.
                #   故此处不再 fields["全额毛利(RMB)"]=... (写公式字段会报错).
        else:
            currencies_missing_fx.append(currency)
        fs = _to_ms(r.get("first_seen")); ls = _to_ms(r.get("last_seen"))
        if fs: fields["首次销售日"] = fs
        if ls: fields["最后销售日"] = ls
        records.append({"fields": fields})

    # Emit rows for advertised-but-unsold SKUs
    sold_skus = {r["sku"] for r in rows}
    for sku, m in ad_sku_metrics.items():
        if sku in sold_skus:
            continue
        cost_local = m.get("cost", 0.0)
        clicks = int(m.get("clicks", 0))
        prints_ = int(m.get("prints", 0))
        direct_local = m.get("direct_amount", 0.0)
        total_local = m.get("total_amount", 0.0)
        fx_ad = _positive_finite(fx_map.get(ad_currency)) or 0
        if cost_local > 0 and not fx_ad:
            currencies_missing_fx.append(ad_currency)
        cost_rmb = cost_local * fx_ad if fx_ad else 0
        prod = products.get(lingxing.resolve_erp_sku(sku)) or {}
        title = prod.get("product_name") or "(advertised but no sale)"
        fields = {
            "SKU": sku,
            "平台": "Mercado Libre",
            "店铺": SHOP_LABEL[seller_id],
            "周期": period,
            "订单数": 0, "件数": 0,
            "币种": ad_currency,
            "营收(原币)": 0,
            "广告费(原币)": round(cost_local, 2),
            "广告展示": prints_,
            "广告点击": clicks,
            "CTR": round(clicks / prints_, 4) if prints_ else 0,
            "CPC(原币)": round(cost_local / clicks, 4) if clicks else 0,
            "广告直接销售(原币)": round(direct_local, 2),
            "ML ROAS": round(total_local / cost_local, 2) if cost_local else 0,
            "广告费(RMB)": round(cost_rmb, 2),
            "我的汇率": round(fx_ad, 4) if fx_ad else 0,
            "营收(RMB)": 0,
            "全额毛利(RMB)": round(-cost_rmb, 2),
            "商品标题": title,
            "数据拉取时间": pulled_at_ms,
        }
        records.append({"fields": fields})

    # If unallocated ad spend > 0 (item_id missing from cache + ML lookup failed), emit synthetic row
    if ad_unallocated_cost > 0:
        unalloc_fx = _positive_finite(fx_map.get(ad_currency)) or 0
        if not unalloc_fx:
            currencies_missing_fx.append(ad_currency)
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
            "全额毛利(RMB)": round(-unalloc_rmb, 2),
            "商品标题": "未归因广告花费 (item_id 无 SKU)",
            "数据拉取时间": pulled_at_ms,
        }})

    # Product Ads stays on the occurrence-month API above. The billing Product
    # Ads amount is evidence only and is deliberately not written again.
    billing_fx = _positive_finite(fx_map.get(billing_currency)) or 0
    billing_values = {
        "_display_ads": (
            "Display Ads（账单费用日期口径）",
            "广告费(原币)",
            "广告费(RMB)",
            float(billing_adjustments.get("display_ads") or 0),
        ),
        "_full_fees": (
            "Full 仓储及违规费用（账单费用日期口径）",
            None,
            "Full仓储费(RMB)",
            float(billing_adjustments.get("full_fees") or 0),
        ),
        "_return_fees": (
            "退货处理费（账单费用日期口径）",
            "退款金额(原币)",
            "退款金额(RMB)",
            float(billing_adjustments.get("return_fees") or 0),
        ),
        "_other_platform_fees": (
            "其他已识别平台费用（账单费用日期口径）",
            "ML佣金(原币)",
            "ML佣金(RMB)",
            float(billing_adjustments.get("other_platform_fees") or 0),
        ),
    }
    for synthetic_sku, (title, local_field, rmb_field, local_value) in billing_values.items():
        if abs(local_value) <= 0.000001:
            continue
        if not billing_fx:
            currencies_missing_fx.append(billing_currency)
        fields = {
            "SKU": synthetic_sku,
            "平台": "Mercado Libre",
            "店铺": SHOP_LABEL[seller_id],
            "周期": period,
            "订单数": 0,
            "件数": 0,
            "币种": billing_currency,
            "营收(原币)": 0,
            "营收(RMB)": 0,
            "我的汇率": round(billing_fx, 4) if billing_fx else 0,
            rmb_field: round(local_value * billing_fx, 2) if billing_fx else 0,
            "商品标题": title,
            "数据拉取时间": pulled_at_ms,
        }
        if local_field:
            fields[local_field] = round(local_value, 2)
        records.append({"fields": fields})

    ad_total_local_value = round(
        sum(v.get("cost", 0.0) for v in ad_sku_metrics.values())
        + ad_unallocated_cost
        + float(billing_adjustments.get("display_ads") or 0),
        2,
    )
    ad_total_rmb_value = round(
        sum(float((record.get("fields") or {}).get("广告费(RMB)") or 0) for record in records),
        2,
    )
    full_total_rmb_value = round(
        sum(float((record.get("fields") or {}).get("Full仓储费(RMB)") or 0) for record in records),
        2,
    )
    refund_total_local_value = round(
        sum(float((record.get("fields") or {}).get("退款金额(原币)") or 0) for record in records),
        2,
    )
    refund_total_rmb_value = round(
        sum(float((record.get("fields") or {}).get("退款金额(RMB)") or 0) for record in records),
        2,
    )
    commission_total_local_value = round(
        sum(float((record.get("fields") or {}).get("ML佣金(原币)") or 0) for record in records),
        2,
    )
    commission_total_rmb_value = round(
        sum(float((record.get("fields") or {}).get("ML佣金(RMB)") or 0) for record in records),
        2,
    )
    units_total_value = int(sum(
        float((record.get("fields") or {}).get("件数") or 0) for record in records
    ))
    revenue_local_total_value = round(
        sum(float((record.get("fields") or {}).get("营收(原币)") or 0) for record in records),
        2,
    )
    shipping_local_total_value = round(
        sum(float((record.get("fields") or {}).get("物流费(原币)") or 0) for record in records),
        2,
    )
    vat_local_total_value = round(
        sum(float((record.get("fields") or {}).get("VAT/税费(原币)") or 0) for record in records),
        2,
    )
    if not commit:
        return {
            "status": "preview",
            "commit": False,
            "seller_id": seller_id,
            "shop": SHOP_LABEL[seller_id],
            "month": month,
            "period": period,
            "rows_previewed": len(records),
            "cached_orders_total": len(cached_rows),
            "units_total": units_total_value,
            "revenue_local_total": revenue_local_total_value,
            "commission_local_total": commission_total_local_value,
            "shipping_local_total": shipping_local_total_value,
            "vat_local_total": vat_local_total_value,
            "refund_local_total": refund_total_local_value,
            "unique_skus": len(rows),
            "advertiser_id": ad_advertiser_id,
            "ad_currency": ad_currency,
            "ad_items_count": ad_items_count,
            "ad_attributed_skus": len(ad_sku_metrics),
            "ad_total_local": ad_total_local_value,
            "ad_total_rmb": ad_total_rmb_value,
            "ad_unallocated_local": round(ad_unallocated_cost, 2),
            "lingxing_product_count": len(products),
            "lingxing_error": lingxing_error,
            "missing_purchase_cost_skus": sorted(set(skus_missing_cost)),
            "missing_fx_currencies": sorted(set(currencies_missing_fx)),
            "shipping_costs_required": len(order_shipments),
            "shipping_costs_fetched": shipping_costs_fetched,
            "shipping_costs_missing": shipping_skipped,
            "billing_period_keys": billing_adjustments.get("period_keys") or [],
            "billing_raw_details": int(billing_adjustments.get("raw_details") or 0),
            "billing_adjustment_count": int(billing_adjustments.get("detail_count") or 0),
            "billing_display_ads_local": round(float(billing_adjustments.get("display_ads") or 0), 2),
            "billing_full_fees_local": round(float(billing_adjustments.get("full_fees") or 0), 2),
            "billing_return_fees_local": round(float(billing_adjustments.get("return_fees") or 0), 2),
            "billing_other_platform_fees_local": round(
                float(billing_adjustments.get("other_platform_fees") or 0), 2
            ),
            "billing_product_ads_ignored": round(float(billing_adjustments.get("product_ads_ignored") or 0), 2),
            "ad_rows": [
                {
                    "sku": fields.get("SKU"),
                    "ad_local": fields.get("广告费(原币)", 0),
                    "ad_rmb": fields.get("广告费(RMB)", 0),
                }
                for fields in (record.get("fields") or {} for record in records)
                if fields.get("广告费(原币)")
            ],
        }

    # Fail closed before touching the production Base.  A temporary Lingxing
    # empty response used to replace otherwise valid monthly rows with blank
    # purchase costs, which made gross profit look materially too high.
    if currencies_missing_fx:
        missing_fx = sorted(set(currencies_missing_fx))
        raise HTTPException(
            422,
            "汇率未完整取得；本次未写入，原报表数据保持不变。"
            f" missing_currencies={missing_fx}"
            + (f" lingxing_error={lingxing_error}" if lingxing_error else ""),
        )
    if skus_missing_cost:
        missing = sorted(set(skus_missing_cost))
        raise HTTPException(
            422,
            "采购成本未完整取得；本次未写入，原报表数据保持不变。"
            f" missing_skus={missing[:20]}"
            + (f" lingxing_error={lingxing_error}" if lingxing_error else ""),
        )
    if shipping_skipped:
        raise HTTPException(
            503,
            "物流费未完整取得；本次未写入，已取得的数据保留在缓存中，可安全重试。"
            f" required={len(order_shipments)} fetched={shipping_costs_fetched} missing={shipping_skipped}",
        )

    from app import ml_close
    await ml_close.invalidate_ab_verification(period, "local_monthly_sync")

    # Safe replace: create the fresh seller/month set first, then delete the old set.
    # If old-row deletion fails, remove the newly created rows to preserve the old snapshot.
    shop_label = SHOP_LABEL[seller_id]
    feishu_token = await _feishu_tenant_token()
    async with httpx.AsyncClient(timeout=30) as client:
        sr_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_APP_TOKEN}/tables/{FEISHU_BASE_TABLE_ID}/records/search"
        create_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_APP_TOKEN}/tables/{FEISHU_BASE_TABLE_ID}/records/batch_create"
        delete_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_APP_TOKEN}/tables/{FEISHU_BASE_TABLE_ID}/records/batch_delete"
        update_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_APP_TOKEN}/tables/{FEISHU_BASE_TABLE_ID}/records/batch_update"
        fs_headers = {
            "Authorization": f"Bearer {feishu_token}",
            "Content-Type": "application/json",
        }
        async def _search_scope(phase: str, target_period: str = period) -> list[dict]:
            sr_body = {"filter": {"conjunction": "and", "conditions": [
                {"field_name": "店铺", "operator": "is", "value": [shop_label]},
                {"field_name": "周期", "operator": "is", "value": [target_period]},
            ]}}
            items: list[dict] = []
            page_token: str | None = None
            while True:
                sr_params = {"page_size": 500}
                if page_token:
                    sr_params["page_token"] = page_token
                sr = await client.post(
                    sr_url,
                    params=sr_params,
                    headers=fs_headers,
                    json=sr_body,
                )
                sr_payload = sr.json()
                if sr.status_code != 200 or sr_payload.get("code") != 0:
                    raise HTTPException(
                        502,
                        f"feishu {phase} lookup failed: status={sr.status_code} code={sr_payload.get('code')}",
                    )
                sr_data = sr_payload.get("data", {})
                items.extend(sr_data.get("items") or [])
                if not sr_data.get("has_more"):
                    return items
                page_token = sr_data.get("page_token")
                if not page_token:
                    raise HTTPException(502, f"feishu {phase} lookup failed: pagination token missing")

        existing_items = await _search_scope("pre-write")
        existing_ids = [it["record_id"] for it in existing_items if it.get("record_id")]
        if existing_ids and not preserve_existing_as:
            raise HTTPException(
                409,
                "当前周期已有数据；必须传 preserve_existing_as 先归档旧版，"
                "本次未创建、删除或覆盖任何记录",
            )
        if existing_ids:
            archive_items = await _search_scope("archive-precheck", preserve_existing_as)
            if archive_items:
                raise HTTPException(
                    409,
                    f"旧版证据周期已存在：{preserve_existing_as}；未修改任何数据",
                )
        # Preserve operator-supplied logistics costs across a monthly rebuild.
        # Missing and explicit zero are different: zero is a confirmed value and
        # must survive just like any positive amount.
        existing_costs_by_sku: dict[str, dict] = {}
        for item in existing_items:
            old_fields = item.get("fields") or {}
            sku = str(old_fields.get("SKU") or "").strip()
            if not sku:
                continue
            preserved = {
                field: old_fields[field]
                for field in ("头程成本(RMB)", "海外仓成本(RMB)")
                if field in old_fields and old_fields[field] not in (None, "")
            }
            if preserved:
                existing_costs_by_sku[sku] = preserved
        for record in records:
            new_fields = record.get("fields") or {}
            preserved = existing_costs_by_sku.get(str(new_fields.get("SKU") or "").strip())
            if preserved:
                new_fields.update(preserved)
        if len(existing_ids) > 500 or len(records) > 500:
            raise HTTPException(502, "feishu safe replace limit exceeded; no rows were changed")

        cr = await client.post(
            create_url,
            headers=fs_headers,
            json={"records": records},
        )
        cr_payload = cr.json()
        if cr.status_code != 200 or cr_payload.get("code") != 0:
            raise HTTPException(
                502,
                f"feishu create failed; old rows preserved: status={cr.status_code} code={cr_payload.get('code')}",
            )
        created_ids = [
            it["record_id"]
            for it in (cr_payload.get("data", {}).get("records") or [])
            if it.get("record_id")
        ]
        if len(created_ids) != len(records):
            rollback_ok = False
            if created_ids:
                rb = await client.post(delete_url, headers=fs_headers, json={"records": created_ids})
                rb_payload = rb.json()
                rollback_ok = rb.status_code == 200 and rb_payload.get("code") == 0
            raise HTTPException(
                502,
                "feishu create response incomplete; "
                f"known-new-row rollback={'ok' if rollback_ok else 'failed'}",
            )

        async def _relabel(record_ids: list[str], target_period: str, phase: str) -> None:
            if not record_ids:
                return
            response = await client.post(
                update_url,
                headers=fs_headers,
                json={"records": [
                    {"record_id": record_id, "fields": {"周期": target_period}}
                    for record_id in record_ids
                ]},
            )
            payload = response.json()
            if response.status_code != 200 or payload.get("code") != 0:
                raise HTTPException(
                    502,
                    f"feishu {phase} failed: status={response.status_code} code={payload.get('code')}",
                )

        def _verify_scope(items: list[dict], phase: str) -> None:
            if len(items) != len(records):
                raise HTTPException(
                    502,
                    f"feishu {phase} mismatch: expected_rows={len(records)} actual_rows={len(items)}",
                )
            financial_expectations = [
                ("广告费(原币)", ad_total_local_value),
                ("广告费(RMB)", ad_total_rmb_value),
                ("Full仓储费(RMB)", full_total_rmb_value),
                ("退款金额(原币)", refund_total_local_value),
                ("退款金额(RMB)", refund_total_rmb_value),
            ]
            if abs(float(billing_adjustments.get("other_platform_fees") or 0)) > 0.000001:
                financial_expectations.extend([
                    ("ML佣金(原币)", commission_total_local_value),
                    ("ML佣金(RMB)", commission_total_rmb_value),
                ])
            for preserved_field in ("头程成本(RMB)", "海外仓成本(RMB)"):
                financial_expectations.append((
                    preserved_field,
                    round(sum(
                        float((record.get("fields") or {}).get(preserved_field) or 0)
                        for record in records
                    ), 2),
                ))
            for metric_key, expected in financial_expectations:
                actual = round(
                    sum(float((it.get("fields") or {}).get(metric_key) or 0) for it in items),
                    2,
                )
                if abs(actual - expected) > 0.05:
                    raise HTTPException(
                        502,
                        f"feishu {phase} mismatch: field={metric_key} expected={expected} actual={actual}",
                    )

        # Verify the fresh set while the old rows still exist. Any failure here can
        # safely roll back only the new rows and leave the old seller/month snapshot intact.
        try:
            after_create_items = await _search_scope("pre-delete")
            created_id_set = set(created_ids)
            created_items = [it for it in after_create_items if it.get("record_id") in created_id_set]
            _verify_scope(created_items, "pre-delete")
        except HTTPException as verify_error:
            rb = await client.post(delete_url, headers=fs_headers, json={"records": created_ids})
            rb_payload = rb.json()
            rollback_ok = rb.status_code == 200 and rb_payload.get("code") == 0
            raise HTTPException(
                502,
                f"{verify_error.detail}; new-row rollback={'ok' if rollback_ok else 'failed'}",
            ) from verify_error

        archived_existing = False
        if existing_ids:
            try:
                await _relabel(existing_ids, preserve_existing_as, "archive old snapshot")
                archived_existing = True
            except HTTPException as archive_error:
                rb = await client.post(delete_url, headers=fs_headers, json={"records": created_ids})
                rb_payload = rb.json()
                rollback_ok = rb.status_code == 200 and rb_payload.get("code") == 0
                raise HTTPException(
                    502,
                    f"{archive_error.detail}; new-row rollback={'ok' if rollback_ok else 'failed'}",
                ) from archive_error

        try:
            verified_items = await _search_scope("read-back")
            _verify_scope(verified_items, "read-back")
            if archived_existing:
                archived_items = await _search_scope("archive-read-back", preserve_existing_as)
                archived_ids = {
                    item.get("record_id") for item in archived_items if item.get("record_id")
                }
                if archived_ids != set(existing_ids):
                    raise HTTPException(
                        502,
                        f"feishu archive read-back mismatch: expected_rows={len(existing_ids)} "
                        f"actual_rows={len(archived_ids)}",
                    )
        except HTTPException as verify_error:
            recovery_errors = []
            rb = await client.post(delete_url, headers=fs_headers, json={"records": created_ids})
            rb_payload = rb.json()
            if rb.status_code != 200 or rb_payload.get("code") != 0:
                recovery_errors.append("new-row rollback failed")
            if archived_existing:
                try:
                    await _relabel(existing_ids, period, "restore old snapshot")
                    restored_items = await _search_scope("restore-read-back", period)
                    restored_ids = {
                        item.get("record_id") for item in restored_items if item.get("record_id")
                    }
                    if restored_ids != set(existing_ids):
                        raise HTTPException(
                            502,
                            f"feishu restore read-back mismatch: expected_rows={len(existing_ids)} "
                            f"actual_rows={len(restored_ids)}",
                        )
                except HTTPException as restore_error:
                    recovery_errors.append(str(restore_error.detail))
            suffix = " recovery=ok" if not recovery_errors else f" recovery_errors={recovery_errors}"
            raise HTTPException(502, f"{verify_error.detail};{suffix}") from verify_error

    try:
        from app import ml_close

        await ml_close.clear_advertising_failure(
            period,
            shop_label,
            success_started_at=sync_started_at,
        )
    except Exception as clear_error:
        print(
            f"[ERROR] failed to clear advertising fetch failure: "
            f"seller_id={seller_id} month={month} error={type(clear_error).__name__}"
        )

    return {"status": "synced", "seller_id": seller_id, "shop": SHOP_LABEL[seller_id],
            "month": month, "period": period, "rows_written": len(records),
            "rows_replaced": len(existing_ids),
            "rows_archived": len(existing_ids) if archived_existing else 0,
            "old_version_period": preserve_existing_as if archived_existing else None,
            "rows_verified": len(verified_items),
            "cached_orders_total": len(cached_rows), "unique_skus": len(rows),
            "units_total": units_total_value,
            "revenue_local_total": revenue_local_total_value,
            "commission_local_total": commission_total_local_value,
            "shipping_local_total": shipping_local_total_value,
            "vat_local_total": vat_local_total_value,
            "refund_local_total": refund_total_local_value,
            "lingxing_products_loaded": len(products),
            "lingxing_error": lingxing_error,
            "skus_missing_cost": skus_missing_cost,
            "advertiser_id": ad_advertiser_id,
            "ad_currency": ad_currency,
            "ad_error": ad_error,
            "ad_items_count": ad_items_count,
            "ad_attributed_skus": len(ad_sku_metrics),
            "ad_total_local": ad_total_local_value,
            "ad_total_rmb": ad_total_rmb_value,
            "ad_unallocated_local": round(ad_unallocated_cost, 2),
            "billing_period_keys": billing_adjustments.get("period_keys") or [],
            "billing_raw_details": int(billing_adjustments.get("raw_details") or 0),
            "billing_adjustment_count": int(billing_adjustments.get("detail_count") or 0),
            "billing_display_ads_local": round(float(billing_adjustments.get("display_ads") or 0), 2),
            "billing_full_fees_local": round(float(billing_adjustments.get("full_fees") or 0), 2),
            "billing_return_fees_local": round(float(billing_adjustments.get("return_fees") or 0), 2),
            "billing_other_platform_fees_local": round(
                float(billing_adjustments.get("other_platform_fees") or 0), 2
            ),
            "billing_product_ads_ignored": round(float(billing_adjustments.get("product_ads_ignored") or 0), 2),
            "vat_rate": vat_rate,
            "site_id_inferred": site_id_for_vat,
            "bitable_url": f"https://u1wpma3xuhr.feishu.cn/base/{FEISHU_BASE_APP_TOKEN}"}


@app.post("/admin/backfill-orders", dependencies=[Depends(require_service_token)])
async def admin_backfill_orders(seller_id: int, recent_n: int = 200, parent_user_id: int = 0,
                                month: str | None = None, max_detail_fetch: int = 40,
                                refresh_after: int | None = None):
    """One-time historical fill: pull orders into ml_order_cache, no Feishu write.

    Two modes:
      - recent_n (default): pull the most-recent N orders. Good for live / near-now.
      - month=YYYY-MM: DATE-WINDOWED — page ALL orders in that month, fetching up to
        max_detail_fetch NEW details per call (cache-hits are free). recent_n cannot reach a
        past high-volume month's early orders (they are no longer "recent"); the window can.
        Call repeatedly across ML cooldowns until new_fetches=0 & capped=false to fully warm
        the cache, then run /report/sync-feishu-monthly.
    Reuses _report_sku_recent_impl which already routes CBT vs local endpoints + caches detail.
    """
    parent = parent_user_id or seller_id
    date_from = date_to = None
    if month:
        if refresh_after is None or refresh_after <= 0:
            raise HTTPException(
                400,
                "monthly backfill requires refresh_after=<stable unix timestamp> "
                "so every mutable order detail is refreshed before reconciliation",
            )
        if refresh_after > int(time.time()) + 30:
            raise HTTPException(
                400,
                "refresh_after is ahead of the service clock; use a stable cutoff "
                "at least 10 minutes before the caller clock to tolerate clock skew",
            )
        yyyy, mm = (int(x) for x in month.split("-"))
        # The search API filters by date_created and can return records across a
        # civil-month boundary. Pull the previous month plus a two-day tail, then
        # derive the authoritative month scope from site-local date_closed below.
        site_timezones = {
            2378517428: "America/Sao_Paulo",
            3383185411: "America/Mexico_City",
        }
        if seller_id not in site_timezones:
            raise HTTPException(400, f"monthly backfill is not configured for seller_id={seller_id}")
        site_timezone = site_timezones[seller_id]
        tz = ZoneInfo(site_timezone)
        py, pm = (yyyy - 1, 12) if mm == 1 else (yyyy, mm - 1)
        search_start = datetime(py, pm, 1, tzinfo=tz)
        ty, tm = (yyyy + 1, 1) if mm == 12 else (yyyy, mm + 1)
        month_end = datetime(ty, tm, 1, tzinfo=tz)
        search_end = month_end + timedelta(days=2)
        date_from = search_start.isoformat(timespec="milliseconds")
        date_to = search_end.isoformat(timespec="milliseconds")
    agg = await _report_sku_recent_impl(
        seller_id, recent_n, parent,
        date_from=date_from, date_to=date_to,
        max_detail_fetch=(max_detail_fetch if month else None),
        refresh_after=(refresh_after if month else None),
        scope_month=(month if month else None),
        scope_timezone=(site_timezone if month else None),
    )
    cache_extras_pruned = 0
    month_scope_replaced = False
    if month:
        platform_ids = {int(order_id) for order_id in (agg.get("_platform_order_ids") or [])}
        month_scope_ids = {int(order_id) for order_id in (agg.get("_month_scope_order_ids") or [])}
        platform_total = agg.get("platform_total")
        month_scope_total = agg.get("month_scope_total")
        complete_detail_pass = (
            isinstance(platform_total, int)
            and len(platform_ids) == platform_total
            and isinstance(month_scope_total, int)
            and len(month_scope_ids) == month_scope_total
            and not agg.get("capped")
            and not agg.get("skipped_429")
            and not agg.get("skipped_other")
            and agg.get("orders_with_detail") == platform_total
        )
        if complete_detail_pass:
            scope_count = await db.cache_replace_month_scope(seller_id, month, sorted(month_scope_ids))
            scoped_rows = await db.cache_list_orders_for_scope(seller_id, month)
            if scope_count != month_scope_total or len(scoped_rows) != month_scope_total:
                raise HTTPException(
                    502,
                    f"month scope reconciliation failed search={platform_total} month={month_scope_total} "
                    f"scope={scope_count} cached={len(scoped_rows)}",
                )
            agg["cached_month_unique"] = len(scoped_rows)
            month_scope_replaced = True
    # Discard aggregation; the side-effect of cache_put_order is what we want.
    return {"status": "backfilled", "seller_id": seller_id, "parent_user_id": parent,
            "mode": ("month:" + month) if month else f"recent_{recent_n}",
            "window_orders": agg.get("packs_returned"),
            "platform_total": agg.get("platform_total"),
            "month_scope_total": agg.get("month_scope_total"),
            "month_scope_unclosed_excluded": agg.get("month_scope_unclosed_excluded"),
            "orders_with_detail": agg.get("orders_with_detail"),
            "cached_month_unique": agg.get("cached_month_unique"),
            "cache_extras_pruned": cache_extras_pruned,
            "month_scope_replaced": month_scope_replaced,
            "refresh_after": refresh_after,
            "cache_hits": agg.get("cache_hits"),
            "new_fetches": agg.get("new_fetches"),
            "skipped_429": agg.get("skipped_429"),
            "skipped_other": agg.get("skipped_other"),
            "detail_failures": agg.get("detail_failures"),
            "partial_details": agg.get("partial_details"),
            "capped": agg.get("capped"),
            "note": "Repeat until new_fetches=0 & capped=false, then /report/sync-feishu-monthly."}


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
