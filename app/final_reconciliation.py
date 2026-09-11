"""Durable final-reconciliation review ledger, separate from operating close.

No report, payroll, or commission writes occur here. A finance decision is not
a published report: publication must be independently verified downstream.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager

from app.final_billing import month_bounds


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_final_batches (
 batch_id TEXT PRIMARY KEY, month TEXT NOT NULL, mode TEXT NOT NULL,
 revision TEXT NOT NULL, baseline_hash TEXT NOT NULL, evidence TEXT NOT NULL,
 state TEXT NOT NULL, ops_actor TEXT NOT NULL, finance_actor TEXT NOT NULL,
 decision TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL,
 UNIQUE(month, mode)
);
CREATE TABLE IF NOT EXISTS ml_final_cards (
 message_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, revision TEXT NOT NULL,
 stage TEXT NOT NULL, actor TEXT NOT NULL, nonce_hash TEXT NOT NULL,
 result TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_final_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
 revision TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 message_id TEXT NOT NULL, result TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_final_feedback (
 message_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
 delivered_at INTEGER, created_at INTEGER NOT NULL
);
"""


class ReviewLedger:
    def __init__(self, path):
        self.path = str(path)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, batch_id):
        with self._connect() as c:
            row = c.execute("SELECT * FROM ml_final_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise ValueError("核销批次不存在")
        result = dict(row)
        result["evidence"] = json.loads(result["evidence"])
        result["decision"] = json.loads(result["decision"])
        return result

    def stage(self, month, baseline_hash, evidence, *, ops_actor, finance_actor, mode="test"):
        """Called only with a verifier-produced evidence package, never card fields.

        Production integration must verify the underlying official sources before
        calling this method; there is deliberately no public 'set AB=true' API.
        """
        month_bounds(month)
        if mode not in {"test", "production"} or not _digest(baseline_hash):
            raise ValueError("批次模式或经营暂结版本错误")
        if not ops_actor or not finance_actor:
            raise ValueError("运营和财务确认人必须明确")
        if mode == "production" and ops_actor == finance_actor:
            raise ValueError("正式核销必须由运营和财务两人分别确认")
        if not isinstance(evidence, dict) or evidence.get("month") != month:
            raise ValueError("核对证据月份不匹配")
        checks = evidence.get("checks")
        required = {"closed_coverage", "source_ab", "fee_mapping", "report_readback"}
        ready = isinstance(checks, dict) and all(checks.get(k) is True for k in required)
        ready = ready and not evidence.get("blockers") and _digest(evidence.get("source_hash"))
        ready = ready and _digest(evidence.get("candidate_hash")) and bool(evidence.get("difference_report_url"))
        revision = _hash({"baseline": baseline_hash, "evidence": evidence,
                          "ops_actor": ops_actor, "finance_actor": finance_actor, "mode": mode})
        batch_id = f"ml-final:{mode}:{month}"
        now = int(time.time())
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute("SELECT * FROM ml_final_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if prior:
                if prior["baseline_hash"] != baseline_hash:
                    raise ValueError("经营暂结基准已锁定，禁止静默替换")
                if prior["revision"] == revision:
                    return self._result(prior, duplicate=True)
                if prior["state"] == "published":
                    raise ValueError("已发布核销批次不能覆盖；需要另行修订流程")
            state = "ops_pending" if ready else "collecting"
            c.execute("""INSERT INTO ml_final_batches
                (batch_id,month,mode,revision,baseline_hash,evidence,state,ops_actor,finance_actor,decision,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'{}',?)
                ON CONFLICT(batch_id) DO UPDATE SET revision=excluded.revision,evidence=excluded.evidence,
                state=excluded.state,ops_actor=excluded.ops_actor,finance_actor=excluded.finance_actor,
                decision='{}',updated_at=excluded.updated_at""",
                (batch_id, month, mode, revision, baseline_hash, _json(evidence), state, ops_actor, finance_actor, now))
            self._event(c, batch_id, revision, "system", "stage", "", {"state": state})
        return {"batch_id": batch_id, "revision": revision, "state": state, "duplicate": False}

    @staticmethod
    def _result(row, duplicate=False):
        return {"batch_id": row["batch_id"], "revision": row["revision"], "state": row["state"], "duplicate": duplicate}

    @staticmethod
    def _event(c, batch_id, revision, actor, action, message_id, result):
        c.execute("INSERT INTO ml_final_events(batch_id,revision,actor,action,message_id,result,created_at) VALUES(?,?,?,?,?,?,?)",
                  (batch_id, revision, actor, action, message_id, _json(result), int(time.time())))

    def register_card(self, batch_id, revision, stage, message_id, nonce):
        if stage not in {"ops", "finance"} or not message_id or len(nonce) < 24:
            raise ValueError("确认卡登记信息不完整")
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM ml_final_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not row or row["revision"] != revision or row["state"] != stage + "_pending":
                raise ValueError("当前核销批次不允许发送此阶段确认卡")
            existing = c.execute("SELECT * FROM ml_final_cards WHERE message_id=?", (message_id,)).fetchone()
            values = (batch_id, revision, stage, row[stage + "_actor"], _hash(nonce))
            if existing:
                if tuple(existing[k] for k in ("batch_id", "revision", "stage", "actor", "nonce_hash")) != values:
                    raise ValueError("同一卡片不能绑定其他核销版本")
                return
            c.execute("INSERT INTO ml_final_cards(message_id,batch_id,revision,stage,actor,nonce_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                      (message_id, *values, int(time.time())))

    def decide(self, batch_id, revision, message_id, nonce, actor, action, *, note=""):
        if action not in {"confirm", "reject"}:
            raise ValueError("不支持的核销动作")
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM ml_final_batches WHERE batch_id=?", (batch_id,)).fetchone()
            card = c.execute("SELECT * FROM ml_final_cards WHERE message_id=?", (message_id,)).fetchone()
            if not row or not card or card["batch_id"] != batch_id or card["revision"] != revision:
                raise ValueError("未登记或不匹配的确认卡")
            if card["actor"] != actor or card["nonce_hash"] != _hash(nonce):
                raise ValueError("确认人或卡片校验失败")
            if row["revision"] != revision:
                raise ValueError("核销资料已更新，请使用最新确认卡")
            if card["result"]:
                return {**json.loads(card["result"]), "duplicate": True}
            stage = card["stage"]
            if row["state"] != stage + "_pending":
                raise ValueError("确认顺序不符或此阶段已经处理")
            if (action == "reject" or stage == "finance") and not note.strip():
                raise ValueError("请填写退回原因或财务差异处理意见")
            decision = json.loads(row["decision"])
            decision[stage] = {"actor": actor, "action": action, "note": note.strip(), "at": int(time.time())}
            decision["automatic_payroll_or_commission_change"] = False
            state = "rejected" if action == "reject" else "finance_pending" if stage == "ops" else "approved"
            result = {"batch_id": batch_id, "revision": revision, "state": state, "duplicate": False,
                      "published": False, "payroll_changed": False, "commission_changed": False}
            c.execute("UPDATE ml_final_batches SET state=?,decision=?,updated_at=? WHERE batch_id=?",
                      (state, _json(decision), int(time.time()), batch_id))
            c.execute("UPDATE ml_final_cards SET result=? WHERE message_id=?", (_json(result), message_id))
            # Same transaction as the decision: a crash cannot lose the PATCH task.
            # Only original-card replacement is queued, never a new message/send.
            c.execute("INSERT INTO ml_final_feedback(message_id,payload,created_at) VALUES(?,?,?)",
                      (message_id, _json({**result, "stage": stage, "action": action,
                                          "actor": actor, "note": note.strip()}), int(time.time())))
            self._event(c, batch_id, revision, actor, action, message_id, result)
            return result

    def pending_feedback(self, limit=50):
        """Replayable original-card replacements; worker must use Finance App.

        Do not acknowledge on HTTP acceptance alone: verify the actual PATCH
        response. Repeating the same replacement is safe after an uncertain result.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("反馈读取数量必须在1到100之间")
        with self._connect() as c:
            rows = c.execute("SELECT message_id,payload FROM ml_final_feedback WHERE delivered_at IS NULL ORDER BY created_at,message_id LIMIT ?", (limit,)).fetchall()
        return [{"message_id": row["message_id"], "payload": json.loads(row["payload"])} for row in rows]

    def acknowledge_feedback(self, message_id):
        """Internal worker only, after successful original-card replacement."""
        with self._connect() as c:
            cursor = c.execute("UPDATE ml_final_feedback SET delivered_at=COALESCE(delivered_at,?) WHERE message_id=?",
                               (int(time.time()), message_id))
            if cursor.rowcount != 1:
                raise ValueError("原卡反馈待办不存在")
