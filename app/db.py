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
