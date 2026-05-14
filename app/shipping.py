"""ML Mercado Envíos shipping cost client.

Discoveries (probed 2026-05-14):
  - GET /shipments/{shipment_id}/costs returns:
      gross_amount (total logistics cost),
      receiver.cost (buyer-paid portion, often 0 due to free-shipping promos),
      senders[0].cost (SELLER actual logistics cost — what we want)

  - The endpoint requires the seller's own token (CBT child sellers use CBT parent token).
  - One ML call per shipment. 5 sellers × ~150 orders/month = ~750 calls/month — heavy.
    Mitigation: SQLite cache (immutable once shipped) + skip-on-error.

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
}


async def fetch_shipping_cost(
    shipment_id: int,
    seller_id: int,
    order_id: int,
    token_user_id: int,
    client: httpx.AsyncClient | None = None,
) -> dict | None:
    """Fetch /shipments/{id}/costs, cache, return sender cost details.

    Returns {sender_cost, gross_amount, currency} or None on failure.
    Cache-first; only hits ML on cache miss.
    """
    cached = await db.cache_get_shipping(shipment_id)
    if cached:
        return {
            "sender_cost": cached.get("sender_cost") or 0,
            "gross_amount": cached.get("gross_amount") or 0,
            "currency": cached.get("currency") or "",
            "from_cache": True,
        }

    row = await db.get_token(token_user_id)
    if not row:
        return None
    headers = {"Authorization": f"Bearer {row['access_token']}"}
    url = f"https://api.mercadolibre.com/shipments/{shipment_id}/costs"

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=20)
    try:
        r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return None
        payload = r.json()
        senders = payload.get("senders") or [{}]
        sender_cost = float(senders[0].get("cost") or 0)
        gross_amount = float(payload.get("gross_amount") or 0)
        # currency: shipment payload has no top-level currency but shipping is in seller country
        # We infer from order context — caller passes via separate channel; default empty
        currency = ""
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
    except Exception:
        return None
    finally:
        if own_client:
            await client.aclose()


async def fetch_many_shipping_costs(
    shipment_keys: list[tuple[int, int, int]],  # [(shipment_id, seller_id, order_id), ...]
    token_user_id: int,
    concurrency: int = 5,
) -> dict[int, dict]:
    """Bulk fetch shipping costs with bounded concurrency, returning {shipment_id: {sender_cost, ...}}."""
    out: dict[int, dict] = {}
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20) as client:
        async def one(sid: int, seller_id: int, order_id: int):
            async with sem:
                r = await fetch_shipping_cost(sid, seller_id, order_id, token_user_id, client=client)
                if r:
                    out[sid] = r

        await asyncio.gather(*[one(*k) for k in shipment_keys], return_exceptions=False)
    return out
