"""ML Direct Sync — FastAPI entrypoint.

M0 骨架：仅暴露 health check 与 OAuth callback 占位。
M1 起逐步加 /sync/orders /sync/items /aggregate/sku-monthly /report/feishu。
"""

import os
from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="ML Direct Sync", version="0.1.0")


@app.get("/")
def root():
    return {"service": "ml-data-sync", "version": "0.1.0", "milestone": "M0"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str | None = None, state: str | None = None):
    """ML OAuth redirect target. M1 实现 code → token 交换。"""
    if not code:
        raise HTTPException(400, "missing code")
    return {
        "status": "received",
        "note": "M0 placeholder — token exchange not implemented yet",
        "code_prefix": code[:6] + "...",
        "state": state,
    }
