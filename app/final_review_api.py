"""Internal Finance Assistant transport; test namespace only until acceptance.

There is intentionally no endpoint accepting a caller's production AB/pass flag.
"""
import secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app import db
from app.final_reconciliation import ReviewLedger


class Sample(BaseModel):
    actor: str = Field(pattern=r"^ou_[a-zA-Z0-9]+$", max_length=100)


class CardRegistration(BaseModel):
    revision: str
    message_id: str = Field(pattern=r"^om_[a-zA-Z0-9]+$", max_length=100)
    nonce: str = Field(min_length=24, max_length=100)


class Decision(CardRegistration):
    actor: str


def router(auth):
    api = APIRouter(prefix="/report/ml-final/test", dependencies=[Depends(auth)])

    def ledger():
        book = ReviewLedger(db.DB_PATH)
        with book._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS ml_final_test_send (revision TEXT PRIMARY KEY, reserved INTEGER NOT NULL)")
        return book

    batch_id = "ml-final:test:2026-08"

    @api.post("/prepare")
    def prepare(body: Sample):
        book = ledger()
        evidence = {"month": "2026-08", "checks": {k: True for k in
                    ("closed_coverage", "source_ab", "fee_mapping", "report_readback")},
                    "source_hash": "a" * 64, "candidate_hash": "b" * 64,
                    "difference_report_url": "test-fixture-only", "blockers": [],
                    "synthetic_test_only": True}
        result = book.stage("2026-08", "c" * 64, evidence,
                            ops_actor=body.actor, finance_actor=body.actor, mode="test")
        # Reuse a registered message; do not send another after the first receipt.
        with book._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute("SELECT message_id FROM ml_final_cards WHERE batch_id=? AND revision=? ORDER BY created_at LIMIT 1",
                              (batch_id, result["revision"])).fetchone()
            reserved = c.execute("INSERT OR IGNORE INTO ml_final_test_send(revision,reserved) VALUES(?,1)", (result["revision"],)).rowcount == 1
        return {**result, "message_id": prior["message_id"] if prior else None,
                "nonce": secrets.token_hex(24) if reserved and not prior else None,
                "delivery_uncertain": not reserved and not prior,
                "production_enabled": False}

    @api.post("/register")
    def register(body: CardRegistration):
        try:
            ledger().register_card(batch_id, body.revision, "ops", body.message_id, body.nonce)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @api.post("/decide")
    def decide(body: Decision):
        try:
            return ledger().decide(batch_id, body.revision, body.message_id, body.nonce, body.actor, "confirm")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/status")
    def status():
        try:
            result = ledger().get(batch_id)
        except ValueError:
            return {"state": "not_started", "production_enabled": False}
        return {k: result[k] for k in ("batch_id", "revision", "state", "decision")}

    @api.get("/feedback")
    def feedback():
        return [item for item in ledger().pending_feedback(100)
                if item["payload"]["batch_id"] == batch_id]

    @api.post("/feedback/{message_id}/ack")
    def acknowledge(message_id: str):
        book = ledger()
        with book._connect() as c:
            card = c.execute("SELECT batch_id FROM ml_final_cards WHERE message_id=?", (message_id,)).fetchone()
        if not card or card["batch_id"] != batch_id:
            raise HTTPException(404, "test card not found")
        book.acknowledge_feedback(message_id)
        return {"ok": True}

    return api
