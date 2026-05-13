"""SQLite layer for token storage.

Schema:
    tokens(user_id, nickname, site_id, access_token, refresh_token,
           expires_at, scope, created_at, updated_at)

SQLite file path comes from SQLITE_PATH env var (default /data/ml_sync.db).
For Zeabur, /data should be mounted as a persistent volume; otherwise the DB is
ephemeral (resets on redeploy) and tokens must be re-seeded.
"""

import os
import time
import aiosqlite
from typing import Any

DB_PATH = os.getenv("SQLITE_PATH", "/data/ml_sync.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    user_id       INTEGER PRIMARY KEY,
    nickname      TEXT,
    site_id       TEXT,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    INTEGER NOT NULL,
    scope         TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_expires_at ON tokens(expires_at);

-- M3 / Phase 1·④: detail caches to avoid re-fetching ML API
CREATE TABLE IF NOT EXISTS ml_order_cache (
    order_id        INTEGER PRIMARY KEY,
    seller_id       INTEGER NOT NULL,
    pack_id         INTEGER,
    date_created    TEXT,           -- ISO string from ML
    date_closed     TEXT,
    paid_amount     REAL,
    currency        TEXT,
    payload         TEXT NOT NULL,  -- full ML JSON (for SKU + items)
    fetched_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ml_order_cache_seller ON ml_order_cache(seller_id);
CREATE INDEX IF NOT EXISTS idx_ml_order_cache_date ON ml_order_cache(date_created);

CREATE TABLE IF NOT EXISTS ml_item_cache (
    item_id         TEXT PRIMARY KEY,
    seller_id       INTEGER,
    sku             TEXT,
    title           TEXT,
    price           REAL,
    currency        TEXT,
    status          TEXT,
    payload         TEXT NOT NULL,
    fetched_at      INTEGER NOT NULL
);
"""


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_token(
    user_id: int,
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
    scope: str | None = None,
    nickname: str | None = None,
    site_id: str | None = None,
) -> None:
    now = int(time.time())
    expires_at = now + int(expires_in)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tokens
                (user_id, nickname, site_id, access_token, refresh_token,
                 expires_at, scope, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                nickname = COALESCE(excluded.nickname, tokens.nickname),
                site_id = COALESCE(excluded.site_id, tokens.site_id),
                access_token = excluded.access_token,
                refresh_token = COALESCE(excluded.refresh_token, tokens.refresh_token),
                expires_at = excluded.expires_at,
                scope = COALESCE(excluded.scope, tokens.scope),
                updated_at = excluded.updated_at
            """,
            (user_id, nickname, site_id, access_token, refresh_token,
             expires_at, scope, now, now),
        )
        await db.commit()


async def get_token(user_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tokens WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_tokens() -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tokens ORDER BY user_id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def list_expiring(within_seconds: int = 1800) -> list[dict[str, Any]]:
    """List tokens that expire within `within_seconds` (default 30 min)."""
    threshold = int(time.time()) + within_seconds
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tokens WHERE expires_at <= ? AND refresh_token IS NOT NULL",
            (threshold,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def cache_get_order(order_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ml_order_cache WHERE order_id = ?", (order_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            import json as _j
            d["_payload"] = _j.loads(d.pop("payload"))
        except Exception:
            d["_payload"] = None
        return d


async def cache_put_order(order_id: int, seller_id: int, payload: dict[str, Any]) -> None:
    import json as _j
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ml_order_cache (order_id, seller_id, pack_id, date_created, date_closed,
                                        paid_amount, currency, payload, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                seller_id = excluded.seller_id,
                pack_id = excluded.pack_id,
                date_created = excluded.date_created,
                date_closed = excluded.date_closed,
                paid_amount = excluded.paid_amount,
                currency = excluded.currency,
                payload = excluded.payload,
                fetched_at = excluded.fetched_at
            """,
            (
                order_id, seller_id, payload.get("pack_id"),
                payload.get("date_created"), payload.get("date_closed"),
                payload.get("paid_amount"),
                ((payload.get("order_items") or [{}])[0].get("currency_id")
                 if payload.get("order_items") else None),
                _j.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        await db.commit()


async def cache_get_item(item_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ml_item_cache WHERE item_id = ?", (item_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            import json as _j
            d["_payload"] = _j.loads(d.pop("payload"))
        except Exception:
            d["_payload"] = None
        return d


async def cache_put_item(item_id: str, payload: dict[str, Any]) -> None:
    import json as _j
    now = int(time.time())
    sku = payload.get("seller_custom_field")
    if not sku:
        for a in (payload.get("attributes") or []):
            if a.get("id") == "SELLER_SKU":
                sku = a.get("value_name") or a.get("value_id")
                break
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ml_item_cache (item_id, seller_id, sku, title, price, currency,
                                       status, payload, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                seller_id = excluded.seller_id,
                sku = excluded.sku,
                title = excluded.title,
                price = excluded.price,
                currency = excluded.currency,
                status = excluded.status,
                payload = excluded.payload,
                fetched_at = excluded.fetched_at
            """,
            (
                item_id, payload.get("seller_id"), sku,
                payload.get("title"), payload.get("price"),
                payload.get("currency_id"), payload.get("status"),
                _j.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        await db.commit()


async def cache_stats() -> dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        row1 = await (await db.execute("SELECT COUNT(*) FROM ml_order_cache")).fetchone()
        row2 = await (await db.execute("SELECT COUNT(*) FROM ml_item_cache")).fetchone()
    return {"orders_cached": row1[0] if row1 else 0, "items_cached": row2[0] if row2 else 0}


def redact(token_row: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive tokens for /admin/tokens listing — keep only metadata + previews."""
    def _preview(s: str | None) -> str | None:
        return f"{s[:14]}...{s[-6:]}" if s and len(s) > 25 else s
    return {
        "user_id": token_row.get("user_id"),
        "nickname": token_row.get("nickname"),
        "site_id": token_row.get("site_id"),
        "access_token_preview": _preview(token_row.get("access_token")),
        "refresh_token_preview": _preview(token_row.get("refresh_token")),
        "has_refresh_token": bool(token_row.get("refresh_token")),
        "expires_at": token_row.get("expires_at"),
        "expires_in_seconds": max(0, (token_row.get("expires_at") or 0) - int(time.time())),
        "scope_has_offline_access": "offline_access" in (token_row.get("scope") or ""),
        "updated_at": token_row.get("updated_at"),
    }
