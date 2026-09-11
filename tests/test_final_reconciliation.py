import tempfile
import unittest
from pathlib import Path

from app.final_reconciliation import ReviewLedger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.ledger = ReviewLedger(self.path)
        self.evidence = {"month": "2026-08", "checks": {k: True for k in ["closed_coverage", "source_ab", "fee_mapping", "report_readback"]},
                         "blockers": [], "source_hash": "a"*64, "candidate_hash": "b"*64,
                         "difference_report_url": "https://example.invalid/test-only"}
        self.nonce = "test-only-nonce-1234567890123456789"

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, evidence=None, **kwargs):
        return self.ledger.stage("2026-08", "c"*64, evidence or self.evidence,
                                 ops_actor="ops", finance_actor="finance", **kwargs)

    def register(self, batch, stage, mid):
        self.ledger.register_card(batch["batch_id"], batch["revision"], stage, mid, self.nonce)

    def decide(self, batch, mid="m1", actor="ops", **kwargs):
        return self.ledger.decide(batch["batch_id"], batch["revision"], mid, self.nonce, actor, "confirm", **kwargs)

    def test_sequential_review_and_no_financial_side_effect(self):
        batch = self.stage()
        with self.assertRaises(ValueError):
            self.register(batch, "finance", "m2")
        self.register(batch, "ops", "m1")
        self.assertEqual(self.decide(batch)["state"], "finance_pending")
        self.register(batch, "finance", "m2")
        with self.assertRaises(ValueError):
            self.decide(batch, "m2", "finance")
        result = self.decide(batch, "m2", "finance", note="另行评估差异，不修改已结算提成")
        self.assertEqual(result["state"], "approved")
        self.assertFalse(result["published"])
        self.assertFalse(result["payroll_changed"])
        self.assertFalse(result["commission_changed"])

    def test_restart_and_duplicate(self):
        batch = self.stage()
        self.register(batch, "ops", "m1")
        self.decide(batch)
        self.ledger = ReviewLedger(self.path)
        self.assertTrue(self.decide(batch)["duplicate"])
        self.assertEqual(self.stage()["state"], "finance_pending")

    def test_wrong_person_or_unregistered_card_blocked(self):
        batch = self.stage()
        with self.assertRaises(ValueError):
            self.decide(batch)
        self.register(batch, "ops", "m1")
        with self.assertRaises(ValueError):
            self.decide(batch, actor="finance")

    def test_missing_evidence_cannot_register(self):
        for check in self.evidence["checks"]:
            evidence = {**self.evidence, "checks": {**self.evidence["checks"], check: False}}
            batch = self.stage(evidence)
            self.assertEqual(batch["state"], "collecting")
            with self.assertRaises(ValueError):
                self.register(batch, "ops", "m1")

    def test_changed_evidence_invalidates_old_card(self):
        batch = self.stage()
        self.register(batch, "ops", "m1")
        self.decide(batch)
        newer = self.stage({**self.evidence, "source_hash": "d"*64})
        self.assertEqual(newer["state"], "ops_pending")
        with self.assertRaisesRegex(ValueError, "资料已更新"):
            self.decide(batch)

    def test_baseline_immutable_and_production_two_people(self):
        self.stage()
        with self.assertRaises(ValueError):
            self.ledger.stage("2026-08", "e"*64, self.evidence, ops_actor="ops", finance_actor="finance")
        with self.assertRaises(ValueError):
            self.ledger.stage("2026-08", "c"*64, self.evidence, ops_actor="same", finance_actor="same", mode="production")

    def test_test_namespace_does_not_touch_production(self):
        test = self.stage()
        production = self.stage(mode="production")
        self.assertNotEqual(test["batch_id"], production["batch_id"])
        self.register(test, "ops", "m1")
        self.decide(test)
        self.assertEqual(self.ledger.get(production["batch_id"])["state"], "ops_pending")

    def test_feedback_survives_restart_and_retries_without_duplicate(self):
        batch = self.stage()
        self.register(batch, "ops", "m1")
        self.decide(batch)
        self.ledger = ReviewLedger(self.path)
        pending = self.ledger.pending_feedback()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["message_id"], "m1")
        self.assertEqual(pending[0]["payload"]["state"], "finance_pending")
        self.decide(batch)
        self.assertEqual(self.ledger.pending_feedback(), pending)
        self.ledger.acknowledge_feedback("m1")
        self.ledger.acknowledge_feedback("m1")
        self.assertEqual(self.ledger.pending_feedback(), [])
        self.assertEqual(self.ledger.get(batch["batch_id"])["state"], "finance_pending")

    def test_rejected_callback_does_not_queue_feedback(self):
        batch = self.stage()
        self.register(batch, "ops", "m1")
        with self.assertRaises(ValueError):
            self.decide(batch, actor="unauthorized")
        self.assertEqual(self.ledger.pending_feedback(), [])
        with self.assertRaises(ValueError):
            self.ledger.acknowledge_feedback("unknown")
        for limit in [0, 101, True, "50"]:
            with self.assertRaises(ValueError):
                self.ledger.pending_feedback(limit)
