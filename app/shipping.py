"""ML Mercado Envíos shipping cost client.

Discoveries (probed 2026-05-14):
  - GET /shipments/{shipment_id}/costs returns:
      gross_amount (total logistics cost),
      receiver.cost (buyer-paid portion, often 0 due to free-shipping promos),
      senders[0].cost (SELLER actual logistics cost — what we want)

  - The endpoint requires the seller's own token (CBT child sellers use CBT parent token).
  - One ML call per shipment. 5 sellers × ~150 orders/month = ~750 calls/month — heavy.
    Mitigation: SQLite cache (immutable once shipped) + skip-on-error.

CBT specifics (probed 2026-05-20, P2.3 专项):
  - Plain /shipments/{id}/costs with CBT parent token returns 401 invalid_caller_id.
  - CBT must use /marketplace/shipments/{id}/costs + header `api-version: 2`
    (same pattern as orders/items/advertising — see坑 #3 #9 in project memory).
  - CBT parent token 1502520822 is shared across orders/shipping/ads → easy 429.
    fetch_many caps concurrency at 2 and retries 429 with exponential backoff.

Used in sync-feishu-monthly:
  - For each cached order with shipping.id, fetch shipping cost via cache-first.
  - Sum senders.cost per SKU as 物流费(原币).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app import db

# Map seller_id → token_user (CBT children use parent CBT token).
SHIPPING_TOKEN_USER: dict[int, int] = {
    1510203792: 1502520822,
    1502236229: 1502520822,
    1407362838: 1407362838,
    1436420028: 1436420028,
    2378517428: 2378517428,
    3383185411: 3383185411,
}

# CBT parent token user_ids — these need /marketplace/ + api-version:2
_CBT_TOKEN_USERS: set[int] = {1502520822}

_429_BACKOFFS = (2.0, 5.0, 12.0)


async def fetch_shipping_cost(
    shipment_id: int,
    seller_id: int,
    order_id: int,
    token_user_id: int,
    client: httpx.AsyncClient | None = None,
) -> dict | None:
    """Fetch shipping costs, cache, return sender cost details.

    Returns {sender_cost, gross_amount, currency} or None on failure.
    Cache-first; treats cached sender_cost<=0 as miss so dirty/empty rows get refetched.
    Retries on 429 with exponential backoff; returns None on other non-200.
    """
    cached = await db.cache_get_shipping(shipment_id)
    if cached and float(cached.get("sender_cost") or 0) > 0:
        return {
            "sender_cost": cached.get("sender_cost") or 0,
            "gross_amount": cached.get("gross_amount") or 0,
            "currency": cached.get("currency") or "",
            "from_cache": True,
        }

    row = await db.get_token(token_user_id)
    if not row:
        return None
    is_cbt = token_user_id in _CBT_TOKEN_USERS
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    if is_cbt:
        url = f"https://api.mercadolibre.com/marketplace/shipments/{shipment_id}/costs"
        headers["api-version"] = "2"
    else:
        url = f"https://api.mercadolibre.com/shipments/{shipment_id}/costs"

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=20)
    try:
        for attempt in range(len(_429_BACKOFFS) + 1):
            try:
                r = await client.get(url, headers=headers)
            except Exception:
                return None
            if r.status_code == 200:
                payload = r.json()
                senders = payload.get("senders") or [{}]
                sender_cost = float(senders[0].get("cost") or 0)
                gross_amount = float(payload.get("gross_amount") or 0)
                currency = payload.get("currency_id") or ""
                await db.cache_put_shipping(
                    shipment_id, seller_id, order_id,
                    sender_cost, gross_amount, currency, payload,
                )
                return {
                    "sender_cost": sender_cost,
                    "gross_amount": gross_amount,
                    "currency": currency,
                    "from_cache": False,
                }
            if r.status_code == 429 and attempt < len(_429_BACKOFFS):
                await asyncio.sleep(_429_BACKOFFS[attempt])
                continue
            # other non-200 (401/403/404/...) — endpoint/permission issue, not retryable
            return None
        return None
    finally:
        if own_client:
            await client.aclose()


async def fetch_many_shipping_costs(
    shipment_keys: list[tuple[int, int, int]],  # [(shipment_id, seller_id, order_id), ...]
    token_user_id: int,
    concurrency: int = 5,
    budget_s: float | None = None,
) -> dict[int, dict]:
    """Bulk fetch shipping costs with bounded concurrency, returning {shipment_id: {sender_cost, ...}}.

    CBT parent token is shared across all CBT operations → cap concurrency at 2 to avoid 429 storms.

    budget_s: optional wall-clock budget for the LIVE-fetch phase. Already-cached shipments
    (sender_cost>0) are seeded instantly with zero ML calls; only the rest are fetched live,
    bounded by budget_s. On timeout we keep whatever completed (each is also persisted to cache)
    and return — so the caller (sync-feishu-monthly) always finishes within bounded time instead
    of hanging indefinitely on CBT's rate-limited shipment endpoint. Uncached shipments fill in
    on subsequent runs from the warmed cache. Without budget_s, behavior is unchanged (full wait).
    """
    if token_user_id in _CBT_TOKEN_USERS:
        concurrency = min(concurrency, 2)
    out: dict[int, dict] = {}
    # Cache-only seed: include already-cached shipments instantly, no ML calls.
    need_live: list[tuple[int, int, int]] = []
    for sid, seller_id, order_id in shipment_keys:
        cached = await db.cache_get_shipping(sid)
        if cached and float(cached.get("sender_cost") or 0) > 0:
            out[sid] = {
                "sender_cost": cached.get("sender_cost") or 0,
                "gross_amount": cached.get("gross_amount") or 0,
                "currency": cached.get("currency") or "",
                "from_cache": True,
            }
        else:
            need_live.append((sid, seller_id, order_id))
    if not need_live:
        return out
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20) as client:
        async def one(sid: int, seller_id: int, order_id: int):
            async with sem:
                r = await fetch_shipping_cost(sid, seller_id, order_id, token_user_id, client=client)
                if r:
                    out[sid] = r

        gathered = asyncio.gather(*[one(*k) for k in need_live], return_exceptions=False)
        if budget_s:
            try:
                await asyncio.wait_for(gathered, timeout=budget_s)
            except asyncio.TimeoutError:
                # Bounded: keep whatever completed (already in `out` + cached); rest fill next run.
                pass
        else:
            await gathered
    return out
