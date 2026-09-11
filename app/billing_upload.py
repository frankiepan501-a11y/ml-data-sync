"""Isolated August intake; no imports of report, close or approval writers."""
import hashlib
import io
import json
import secrets
import sqlite3
import time
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

MAX_FILE = 20 * 1024 * 1024
MAX_BATCH = 200 * 1024 * 1024
SELLERS = {"1502236229": "CBT-FULL", "2378517428": "巴西本土", "3383185411": "MX3 DISTRIBUIDOR VALMIGOZ"}
KINDS = {"charges": "费用明细", "period": "官方账期/单据清单", "support": "补充证明"}
SOURCES = {"historical_official", "synthetic", "unverified"}


def validate_file(name, content):
    if not isinstance(name, str) or not name or len(name) > 180 or any(ord(c) < 32 for c in name) or "/" in name or "\\" in name:
        raise ValueError("文件名不合法")
    if not content or len(content) > MAX_FILE:
        raise ValueError("文件为空或超过20MB")
    suffix = Path(name).suffix.lower()
    signatures = {".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".xlsx": b"PK\x03\x04"}
    if suffix not in signatures or not content.startswith(signatures[suffix]):
        raise ValueError("仅接收原始XLSX、PDF、PNG、JPEG，文件内容须匹配扩展名")
    if suffix == ".xlsx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                items = z.infolist()
                names = [i.filename for i in items]
                if len(items) > 2000 or sum(i.file_size for i in items) > 100 * 1024 * 1024:
                    raise ValueError("Excel解压后超限")
                if len(set(names)) != len(names) or "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
                    raise ValueError("不是有效的Excel工作簿")
                for item in items:
                    p = PurePosixPath(item.filename)
                    if item.flag_bits & 1 or p.is_absolute() or ".." in p.parts or "\\" in item.filename or ":" in item.filename:
                        raise ValueError("Excel含不安全路径或加密内容")
                    low = item.filename.lower()
                    if any(s in low for s in ("vbaproject", "externallinks/", "embeddings/", "activex/")) or p.suffix.lower() in {".exe", ".js", ".vbs", ".bat", ".cmd", ".dll"}:
                        raise ValueError("不接收宏、外部链接或嵌入程序")
                    if low.endswith((".xml", ".rels")):
                        raw = z.read(item).replace(b"\x00", b"").upper()
                        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                            raise ValueError("Excel含不安全XML声明")
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
            raise ValueError("Excel文件损坏或压缩格式不支持，请重新从平台导出原件") from exc


class UploadStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS session(id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
              expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, message_id TEXT);
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
              seller TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, sha TEXT NOT NULL,
              size INTEGER NOT NULL, content BLOB NOT NULL, created INTEGER NOT NULL,
              state TEXT NOT NULL DEFAULT 'queued', result TEXT NOT NULL DEFAULT '{}', lease INTEGER,
              UNIQUE(session_id,seller,kind,sha));
            """)
            c.execute("BEGIN IMMEDIATE")
            if "source" not in {r[1] for r in c.execute("PRAGMA table_info(files)")}:
                c.execute("ALTER TABLE files ADD COLUMN source TEXT NOT NULL DEFAULT 'unverified'")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def prepare(self, now=None):
        now = int(time.time()) if now is None else now
        token = secrets.token_urlsafe(32)
        sid = secrets.token_hex(16)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute("SELECT id,message_id FROM session WHERE revoked=0 AND expires>? LIMIT 1", (now,)).fetchone()
            if prior:
                return {"id": prior["id"], "message_id": prior["message_id"], "delivery_uncertain": not bool(prior["message_id"])}
            c.execute("INSERT INTO session(id,token_hash,expires) VALUES(?,?,?)", (sid, hashlib.sha256(token.encode()).hexdigest(), now + 86400))
        return {"id": sid, "token": token, "expires": now + 86400, "mode": "test", "month": "2026-08"}

    def register(self, sid, mid):
        if not isinstance(mid, str) or not mid.startswith("om_"):
            raise ValueError("消息编号不合法")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT message_id FROM session WHERE id=?", (sid,)).fetchone()
            if not row or row[0] not in (None, mid):
                raise ValueError("批次不存在或已绑定其他卡片")
            c.execute("UPDATE session SET message_id=? WHERE id=?", (mid, sid))

    def revoke(self, sid):
        with self.connect() as c:
            c.execute("UPDATE session SET revoked=1 WHERE id=?", (sid,))

    def authorize(self, c, token, now=None):
        now = int(time.time()) if now is None else now
        if not isinstance(token, str) or len(token) > 100:
            raise PermissionError("上传链接无效或已过期，请联系发送人重新提供")
        row = c.execute("SELECT id FROM session WHERE token_hash=? AND revoked=0 AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), now)).fetchone()
        if not row:
            raise PermissionError("上传链接无效或已过期，请联系发送人重新提供")
        return row[0]

    def save(self, token, seller, kind, name, content, now=None, source="unverified"):
        if source not in SOURCES:
            raise ValueError("测试材料来源类型不合法")
        if seller not in SELLERS or kind not in KINDS:
            raise ValueError("店铺或文件用途不合法")
        with self.connect() as c:
            self.authorize(c, token, now)
        validate_file(name, content)
        sha = hashlib.sha256(content).hexdigest()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            sid = self.authorize(c, token, now)
            prior = c.execute("SELECT id FROM files WHERE session_id=? AND seller=? AND kind=? AND sha=?", (sid, seller, kind, sha)).fetchone()
            if prior:
                return {"id": prior[0], "duplicate": True, "sha256": sha, "saved": True}
            count, size = c.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM files WHERE session_id=?", (sid,)).fetchone()
            if count >= 30 or size + len(content) > MAX_BATCH:
                raise ValueError("本批次超过30个文件或200MB，请先核对已上传清单")
            fid = secrets.token_hex(16)
            c.execute("INSERT INTO files(id,session_id,seller,kind,name,sha,size,content,created) VALUES(?,?,?,?,?,?,?,?,?)", (fid, sid, seller, kind, name, sha, len(content), content, int(time.time()) if now is None else now))
            c.execute("UPDATE files SET source=? WHERE id=?", (source, fid))
            stored = c.execute("SELECT content FROM files WHERE id=?", (fid,)).fetchone()[0]
            if hashlib.sha256(stored).hexdigest() != sha:
                raise ValueError("原件保存核对失败，请重试")
        return {"id": fid, "duplicate": False, "sha256": sha, "saved": True}

    def status(self, token, now=None):
        with self.connect() as c:
            sid = self.authorize(c, token, now)
            rows = [dict(r) for r in c.execute("SELECT id,seller,kind,name,sha,size,created,state,result,source FROM files WHERE session_id=? ORDER BY created,id", (sid,))]
        for row in rows:
            row["result"] = json.loads(row["result"])
            row["same_name_conflict"] = any(other["id"] != row["id"] and other["seller"] == row["seller"] and other["name"] == row["name"] and other["sha"] != row["sha"] for other in rows)
        return {"month": "2026-08", "mode": "test", "files": rows, "sellers": [
            {"id": seller, "label": label, "missing": [KINDS[k] for k in ("charges", "period") if not any(r["seller"] == seller and r["kind"] == k for r in rows)],
             "checks_pending": ["店铺归属待核实", "官方已结束账期覆盖待核实", "单据完整性待核实"]}
            for seller, label in SELLERS.items()], "ready_for_final_confirmation": False}

    def download(self, token, fid, now=None):
        with self.connect() as c:
            sid = self.authorize(c, token, now)
            row = c.execute("SELECT name,content,sha FROM files WHERE id=? AND session_id=?", (fid, sid)).fetchone()
        if not row:
            raise PermissionError("原件不存在或不属于此提交批次")
        if hashlib.sha256(row[1]).hexdigest() != row[2]:
            raise ValueError("原件校验失败，已阻止下载")
        return row[0], row[1]

    def claim(self, now=None):
        now = int(time.time()) if now is None else now
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE files SET state='queued',lease=NULL WHERE state='processing' AND lease<?", (now - 90,))
            if c.execute("SELECT 1 FROM files WHERE state='processing' LIMIT 1").fetchone():
                return None
            row = c.execute("SELECT id FROM files WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE files SET state='processing',lease=? WHERE id=?", (now, row[0]))
            return row[0], now

    def finish(self, fid, lease, result):
        with self.connect() as c:
            c.execute("UPDATE files SET state=?,result=?,lease=NULL WHERE id=? AND state='processing' AND lease=?", (result["state"], json.dumps(result, ensure_ascii=False), fid, lease))
