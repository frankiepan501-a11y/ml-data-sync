"""Test-only portal. Bearer upload grants cannot confirm any financial report."""
import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from app import db
from app.billing_upload import UploadStore, MAX_FILE

PREFIX = "/report/ml-intake/test"
ASSETS = Path(__file__).parent / "intake_web"
STOP = threading.Event()
UPLOAD_LIMIT = asyncio.Semaphore(2)


def store():
    return UploadStore(Path(db.DB_PATH).parent / "ml-intake-test" / "uploads.sqlite")


def token(request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise PermissionError("上传链接无效或已过期")
    return header[7:]


def process_one(book):
    claimed = book.claim()
    if not claimed:
        return False
    fid, lease = claimed
    try:
        completed = subprocess.run([sys.executable, str(Path(__file__).with_name("billing_upload_parse.py")), book.path, fid],
            cwd=str(Path(__file__).parent.parent), capture_output=True, timeout=60, check=True)
        result = json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        result = {"state": "failed", "reason": "解析超过60秒，已停止；原件保留待核实"}
    except (subprocess.SubprocessError, ValueError, OSError):
        result = {"state": "failed", "reason": "解析未完成；原件保留待核实"}
    book.finish(fid, lease, result)
    return True


def start_worker():
    STOP.clear()
    def work():
        while not STOP.wait(2):
            try:
                process_one(store())
            except Exception:
                print("[ML_INTAKE_TEST] queue retry required", flush=True)
    threading.Thread(target=work, daemon=True, name="ml-intake-test").start()


class Registration(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    message_id: str = Field(pattern=r"^om_[a-zA-Z0-9]+$", max_length=100)


def router(auth):
    api = APIRouter(prefix=PREFIX)

    @api.post("/prepare", dependencies=[Depends(auth)])
    def prepare():
        return store().prepare()

    @api.post("/register", dependencies=[Depends(auth)])
    def register(body: Registration):
        store().register(body.id, body.message_id)
        return {"ok": True}

    @api.post("/revoke/{sid}", dependencies=[Depends(auth)])
    def revoke(sid: str):
        store().revoke(sid)
        return {"ok": True}

    @api.get("/upload")
    def page():
        return FileResponse(ASSETS / "index.html", media_type="text/html")

    @api.get("/portal.js")
    def script():
        return FileResponse(ASSETS / "portal.js", media_type="text/javascript")

    @api.get("/status")
    def status(request: Request):
        try:
            return store().status(token(request))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc

    @api.post("/files")
    async def upload(request: Request, seller: str, kind: str, name: str, source: str = "unverified"):
        try:
            grant = token(request)
            await run_in_threadpool(store().status, grant)
            if int(request.headers.get("content-length", "0")) > MAX_FILE:
                raise ValueError("文件超过20MB")
            async with UPLOAD_LIMIT:
                async with asyncio.timeout(90):
                    content = bytearray()
                    async for chunk in request.stream():
                        content.extend(chunk)
                        if len(content) > MAX_FILE:
                            raise ValueError("文件超过20MB")
                    return await run_in_threadpool(store().save, grant, seller, kind, name, bytes(content), source=source)
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        except (ValueError, TimeoutError) as exc:
            raise HTTPException(400, str(exc) or "上传超时，请重试") from exc
        except (sqlite3.Error, OSError) as exc:
            raise HTTPException(503, "保存未完成，请稍后重试并核对清单") from exc

    @api.get("/files/{fid}")
    def download(fid: str, request: Request):
        try:
            name, content = store().download(token(request), fid)
            return Response(content, media_type="application/octet-stream", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return api


def install(app, auth):
    app.include_router(router(auth))
    app.add_event_handler("startup", start_worker)
    app.add_event_handler("shutdown", STOP.set)

    @app.middleware("http")
    async def security(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(PREFIX):
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response
