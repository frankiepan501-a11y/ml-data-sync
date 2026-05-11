"""ML Direct Sync — FastAPI entrypoint.

M1 PoC: OAuth code → token exchange, tokens go to stdout (Zeabur runtime logs).
M2+: persist to SQLite, add /sync/orders /sync/items /aggregate endpoints.
"""

import os
import httpx
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="ML Direct Sync", version="0.2.0")


@app.get("/")
def root():
    return {"service": "ml-data-sync", "version": "0.2.0", "milestone": "M1"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/oauth/authorize-url")
def authorize_url(site: str = "MLM"):
    """Build the ML OAuth authorization URL for a given site (MLM / MLA / MLB / MLC / MCO)."""
    tld_map = {
        "MLM": "com.mx",
        "MLA": "com.ar",
        "MLB": "com.br",
        "MLC": "cl",
        "MCO": "com.co",
    }
    tld = tld_map.get(site.upper(), "com.mx")
    app_id = os.getenv("ML_APP_ID", "")
    redirect = os.getenv("ML_REDIRECT_URI", "")
    if not app_id or not redirect:
        raise HTTPException(500, "ML_APP_ID or ML_REDIRECT_URI not configured")
    import urllib.parse
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": redirect,
    })
    return {"site": site.upper(), "url": f"https://auth.mercadolibre.{tld}/authorization?{qs}"}


@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """ML OAuth callback — exchanges authorization code for access_token + refresh_token.

    M1 PoC: tokens are logged to stdout (visible in Zeabur runtime logs).
    M2: will persist to SQLite + auto-refresh cron.
    """
    if error:
        print(f"[OAUTH ERROR] {error}", flush=True)
        return {"status": "error", "error": error}
    if not code:
        raise HTTPException(400, "missing code parameter")

    app_id = os.getenv("ML_APP_ID")
    app_secret = os.getenv("ML_APP_SECRET")
    redirect = os.getenv("ML_REDIRECT_URI")
    if not (app_id and app_secret and redirect):
        raise HTTPException(500, "ML credentials not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.mercadolibre.com/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect,
            },
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        print(f"[OAUTH FAILED] status={resp.status_code} body={resp.text}", flush=True)
        return {"status": "error", "http_status": resp.status_code, "body": resp.text}

    data = resp.json()
    user_id = data.get("user_id")
    scope = data.get("scope")
    expires_in = data.get("expires_in")

    # M1: dump full tokens to runtime logs (Zeabur owner-only) for retrieval
    print(f"[OAUTH SUCCESS] user_id={user_id} scope={scope} expires_in={expires_in}", flush=True)
    print(f"[OAUTH TOKEN_TYPE] {data.get('token_type')}", flush=True)
    print(f"[OAUTH ACCESS_TOKEN] {data.get('access_token')}", flush=True)
    print(f"[OAUTH REFRESH_TOKEN] {data.get('refresh_token')}", flush=True)

    return {
        "status": "success",
        "user_id": user_id,
        "scope": scope,
        "expires_in": expires_in,
        "note": "Tokens stored server-side. You can close this page.",
    }
