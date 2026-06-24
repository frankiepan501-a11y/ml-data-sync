"""Lingxing ERP client — minimal async wrapper for the 2 endpoints we need.

Used by sync-feishu-monthly to enrich ML orders with cost + FX:
  - /erp/sc/routing/data/local_inventory/productList → sku → cg_price (RMB)
  - /erp/sc/routing/finance/currency/currencyMonth → currency_code → my_rate (vs CNY)

Auth: AES-128-ECB sign over MD5'd sorted-param query string. Token ~2h TTL.

In-memory cache:
  - token: re-fetch if expired
  - products: 5-min TTL (442 SKUs, ~250 KB)
  - fx_rate: 1-h TTL per month
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.parse
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


_LX_BASE = "https://openapi.lingxing.com"

# ML seller_sku (运营在 ML listing 后台填的非规范 SKU) → 领星 ERP SKU 映射。
# 当 ML 后台 listing 命名跟领星产品库不一致时（通常是 CBT 站定制 listing），
# 用这个表把 ML SKU 转回领星 SKU 以查询 cg_price。
#
# 加新映射: 1) Frankie/俊辉确认对应关系 2) 在 Lingxing 里 verify ERP SKU 存在
# 3) 在这里加一行 4) 下次 sync-feishu-monthly 自动生效
SKU_ALIAS_TO_ERP: dict[str, str] = {
    # CBT-FULL Switch YS11pro 手柄系列 — 2026-05-15 俊辉确认
    "MXCFFLFFSCP-TOTKW01": "FF01B-01",  # YS11pro 手柄-白眼款（霍尔摇杆）
    "MXCFFLFFSCP-TOTK":    "FF01A-04",  # YS11pro 手柄-波纹款（霍尔摇杆）
    "MXCFFLFFSCP-TOTKB02": "FF01A-05",  # YS11pro 手柄-魔法阵款（霍尔摇杆）
    # CBT 定制 listing → 产品 ERP SKU(2026-06-24 俊辉双数据点推导: 运单→ML SKU + 同运单 gGxKHQ 填的产品SKU)
    # 俊辉在 gGxKHQ 把 ERP-SKU 标为产品SKU(TZ15/KS37-4/FF05B-01), ML 报表卖的是 listing SKU → 在此映射对齐
    "FB07-7":   "TZ15",      # 2代钢印包套装 (运单 ZSMX2510023684)
    "YS37-01":  "KS37-4",    # KS37 joycon手柄-黑色 (运单 ZSMX2511044430)
    "FF05-1":   "FF05B-01",  # YS11-5 充电底座套装-惊奇款 (运单 ZSMX2512096743)
}


def resolve_erp_sku(ml_sku: str) -> str:
    """Translate ML seller_sku to Lingxing ERP SKU if aliased, else return as-is."""
    return SKU_ALIAS_TO_ERP.get(ml_sku, ml_sku)

# In-memory caches
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}
_products_cache: dict[str, Any] = {"items": None, "expires_at": 0}
_fx_cache: dict[str, dict[str, float]] = {}  # month → {currency: my_rate}


def _app_id() -> str:
    return os.getenv("LINGXING_APP_ID") or ""


def _app_secret() -> str:
    return os.getenv("LINGXING_APP_SECRET") or ""


def _make_sign(params: dict[str, Any]) -> str:
    sorted_keys = sorted(params.keys())
    filtered = [(k, params[k]) for k in sorted_keys if params[k] not in ("", None)]
    sorted_str = "&".join(f"{k}={v}" for k, v in filtered)
    md5 = hashlib.md5(sorted_str.encode()).hexdigest().upper()
    key = _app_id().encode()
    if len(key) < 16:
        key += b"\x00" * (16 - len(key))
    else:
        key = key[:16]
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(md5.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode()


async def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]
    if not _app_id() or not _app_secret():
        raise RuntimeError("LINGXING_APP_ID / LINGXING_APP_SECRET not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{_LX_BASE}/api/auth-server/oauth/access-token",
            data={"appId": _app_id(), "appSecret": _app_secret()},
        )
    r.raise_for_status()
    d = r.json()
    tok = (d.get("data") or {}).get("access_token")
    if not tok:
        raise RuntimeError(f"lingxing token fetch failed: {d}")
    # Lingxing tokens are typically 2h; refresh proactively at 1h45m
    _token_cache["token"] = tok
    _token_cache["expires_at"] = now + 6300
    return tok


async def lx_api(api_path: str, biz_params: dict[str, Any]) -> dict[str, Any]:
    """Async wrapper for Lingxing API. Body is JSON; auth is via query string."""
    token = await _get_token()
    ts = str(int(time.time()))
    common = {"access_token": token, "app_key": _app_id(), "timestamp": ts}
    sp = {**common}
    for k, v in biz_params.items():
        sp[k] = str(v)
    sign = urllib.parse.quote(_make_sign(sp))
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in common.items()) + "&sign=" + sign
    url = f"{_LX_BASE}{api_path}?{qs}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url, json=biz_params, headers={"Content-Type": "application/json"}
        )
    r.raise_for_status()
    return r.json()


async def fetch_all_products() -> dict[str, dict]:
    """Return {sku: product_row} for all products. 5-min cached."""
    now = time.time()
    if _products_cache["items"] is not None and _products_cache["expires_at"] > now:
        return _products_cache["items"]

    all_p: dict[str, dict] = {}
    offset = 0
    page_size = 200
    while True:
        r = await lx_api(
            "/erp/sc/routing/data/local_inventory/productList",
            {"offset": offset, "length": page_size},
        )
        data = r.get("data") or []
        for p in data:
            sku = p.get("sku")
            if sku:
                all_p[sku] = p
        total = r.get("total", 0)
        if offset + page_size >= total or not data:
            break
        offset += page_size

    _products_cache["items"] = all_p
    _products_cache["expires_at"] = now + 300  # 5 min
    return all_p


async def fetch_fx_rate(month: str) -> dict[str, float]:
    """Return {currency_code: my_rate_to_cny} for the given YYYY-MM. 1h cached."""
    cached = _fx_cache.get(month)
    if cached and cached.get("_expires_at", 0) > time.time():
        return cached

    r = await lx_api("/erp/sc/routing/finance/currency/currencyMonth", {"date": month})
    out: dict[str, float] = {}
    for c in r.get("data") or []:
        code = c.get("code")
        rate = c.get("my_rate")
        if code and rate is not None:
            try:
                out[code] = float(rate)
            except (TypeError, ValueError):
                pass
    out["_expires_at"] = time.time() + 3600
    _fx_cache[month] = out
    return out
