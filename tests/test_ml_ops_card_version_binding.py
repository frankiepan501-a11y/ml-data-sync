import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from app import db, main, ml_close


PERIOD = "month_2026-08"
REPORT_HASH = "current-v4-hash"


@asynccontextmanager
async def ready_action_guard(*args, **kwargs):
    yield True


class MlOpsCardVersionBindingTests(unittest.IsolatedAsyncioTestCase):
    def test_both_ops_actions_carry_the_displayed_report_hash(self):
        card = ml_close.build_card(
            "ops_operating",
            {
                "period": PERIOD,
                "month": "2026-08",
                "report_hash": REPORT_HASH,
                "report_url": "https://example.test/v4",
            },
            {"状态": "待运营确认"},
        )
        values = [
            button.get("value")
            for element in card["elements"]
            for button in element.get("actions", [])
            if (button.get("value") or {}).get("action")
            in {"ml_profit_ops_confirm", "ml_profit_ops_reject"}
        ]

        self.assertEqual(2, len(values))
        self.assertEqual({REPORT_HASH}, {value.get("report_hash") for value in values})

    def test_ops_card_without_report_hash_has_no_business_action_buttons(self):
        card = ml_close.build_card(
            "ops_operating",
            {
                "period": PERIOD,
                "month": "2026-08",
                "report_hash": "",
                "report_url": "https://example.test/v4",
            },
            {"状态": "待运营确认"},
        )
        business_actions = [
            button.get("value", {}).get("action")
            for element in card["elements"]
            for button in element.get("actions", [])
            if button.get("value", {}).get("action")
            in {"ml_profit_ops_confirm", "ml_profit_ops_reject"}
        ]

        self.assertEqual([], business_actions)

    def test_health_exposes_the_version_binding_marker(self):
        self.assertIs(
            True,
            main.health().get("ml_ops_card_report_hash_binding_v1_20260915"),
        )

    async def test_missing_or_stale_card_hash_blocks_before_business_writes(self):
        status = {
            "record_id": "rec-status",
            "fields": {
                "状态": "待运营确认",
                "最后卡片 message_id": "om-current",
                "最后结果JSON": "{}",
            },
        }
        summary = {
            "status": "ok",
            "period": PERIOD,
            "month": "2026-08",
            "state": "待运营确认",
            "next_card": "ops_operating",
            "last_error": "",
            "report_hash": REPORT_HASH,
            "ab_verified": False,
            "operating_ready": True,
        }

        cases = (
            (PERIOD, None),
            (PERIOD, "stale-v4-hash"),
            ("month_2026-09", "stale-v4-hash"),
        )
        for period, card_hash in cases:
            for action in ("ml_profit_ops_confirm", "ml_profit_ops_reject"):
                with self.subTest(
                    period=period, action=action, card_hash=card_hash
                ):
                    writer = AsyncMock()
                    sender = AsyncMock(
                        return_value={"data": {"message_id": "om-finance"}}
                    )
                    canceler = AsyncMock()
                    claimer = AsyncMock(
                        return_value={"claimed": True, "status": "processing"}
                    )
                    failer = AsyncMock()
                    payload = {
                        "action": action,
                        "period": period,
                        "message_id": "om-current",
                        "operator_name": "梁俊辉",
                        "operator_id": ml_close.ML_CLOSE_OPS_APPROVER_OPEN_ID,
                        "patch_message": False,
                    }
                    if card_hash is not None:
                        payload["report_hash"] = card_hash

                    with (
                        patch.object(
                            db,
                            "get_active_ml_close_month_work",
                            AsyncMock(return_value=None),
                        ),
                        patch.object(db, "claim_ml_close_action", claimer),
                        patch.object(db, "fail_ml_close_action", failer),
                        patch.object(
                            db,
                            "ml_close_action_finalization_guard",
                            ready_action_guard,
                        ),
                        patch.object(
                            db, "cancel_unified_report_generation", canceler
                        ),
                        patch.object(
                            ml_close,
                            "_tenant_token",
                            AsyncMock(return_value="token"),
                        ),
                        patch.object(
                            ml_close,
                            "_get_status",
                            AsyncMock(return_value=status),
                        ),
                        patch.object(
                            ml_close,
                            "_current_report_hash",
                            AsyncMock(return_value=REPORT_HASH),
                        ),
                        patch.object(
                            ml_close,
                            "_open_ad_failures",
                            AsyncMock(return_value=[]),
                        ),
                        patch.object(
                            ml_close, "audit", AsyncMock(return_value=summary)
                        ),
                        patch.object(
                            ml_close,
                            "_prepare_finance_review",
                            AsyncMock(
                                return_value={
                                    **summary,
                                    "report_url": "https://example.test/v4",
                                    "review_source_hash": REPORT_HASH,
                                }
                            ),
                        ),
                        patch.object(ml_close, "_upsert_status", writer),
                        patch.object(ml_close, "send_card", sender),
                        patch.object(
                            ml_close,
                            "patch_or_fallback",
                            AsyncMock(return_value={}),
                        ),
                    ):
                        result = await ml_close.confirm_action(payload)

                    self.assertEqual("blocked", result["status"])
                    self.assertIn("报表版本", result["reason"])
                    writer.assert_not_awaited()
                    sender.assert_not_awaited()
                    canceler.assert_not_awaited()
                    claimer.assert_not_awaited()
                    failer.assert_not_awaited()

    async def test_old_hashless_card_preserves_historical_compatibility(self):
        claimer = AsyncMock(side_effect=RuntimeError("stop-after-version-gate"))
        current_hash = AsyncMock(return_value=REPORT_HASH)
        with (
            patch.object(
                db, "get_active_ml_close_month_work", AsyncMock(return_value=None)
            ),
            patch.object(db, "claim_ml_close_action", claimer),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(
                ml_close,
                "_get_status",
                AsyncMock(
                    return_value={
                        "record_id": "rec-old",
                        "fields": {
                            "状态": "待运营确认",
                            "最后卡片 message_id": "om-old",
                            "最后结果JSON": "{}",
                        },
                    }
                ),
            ),
            patch.object(ml_close, "_current_report_hash", current_hash),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
        ):
            result = await ml_close.confirm_action(
                {
                    "action": "ml_profit_ops_confirm",
                    "period": "month_2026-07",
                    "message_id": "om-old",
                    "operator_name": "梁俊辉",
                    "operator_id": ml_close.ML_CLOSE_OPS_APPROVER_OPEN_ID,
                    "patch_message": False,
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertIn("月结动作去重台账不可用", result["reason"])
        claimer.assert_awaited_once()
        current_hash.assert_not_awaited()

    async def test_confirm_rejects_when_a_matching_nested_card_becomes_stale(self):
        status = {
            "record_id": "rec-status",
            "fields": {
                "状态": "待运营确认",
                "最后卡片 message_id": "om-current",
                "最后结果JSON": "{}",
            },
        }
        changed = {
            "status": "ok",
            "period": PERIOD,
            "month": "2026-08",
            "state": "待运营确认",
            "next_card": "ops_operating",
            "last_error": "",
            "report_hash": "changed-after-click",
            "ab_verified": False,
            "operating_ready": True,
        }
        writer = AsyncMock()
        sender = AsyncMock()
        canceler = AsyncMock()
        claimer = AsyncMock(
            return_value={"claimed": True, "status": "processing"}
        )
        failer = AsyncMock()
        with (
            patch.object(
                db, "get_active_ml_close_month_work", AsyncMock(return_value=None)
            ),
            patch.object(db, "claim_ml_close_action", claimer),
            patch.object(db, "fail_ml_close_action", failer),
            patch.object(db, "cancel_unified_report_generation", canceler),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value=REPORT_HASH),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=changed)),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "send_card", sender),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action(
                {
                    "value": {
                        "action": "ml_profit_ops_confirm",
                        "period": PERIOD,
                        "report_hash": REPORT_HASH,
                    },
                    "message_id": "om-current",
                    "operator_name": "梁俊辉",
                    "operator_id": ml_close.ML_CLOSE_OPS_APPROVER_OPEN_ID,
                    "patch_message": False,
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertIn("操作前报表内容已变化", result["reason"])
        claimer.assert_awaited_once()
        failer.assert_awaited_once()
        writer.assert_not_awaited()
        sender.assert_not_awaited()
        canceler.assert_not_awaited()

    async def test_reject_rechecks_hash_before_cancelling_generation(self):
        status = {
            "record_id": "rec-status",
            "fields": {
                "状态": "待运营确认",
                "最后卡片 message_id": "om-current",
                "最后结果JSON": "{}",
            },
        }
        writer = AsyncMock()
        canceler = AsyncMock()
        claimer = AsyncMock(
            return_value={"claimed": True, "status": "processing"}
        )
        discarder = AsyncMock()
        audit_mock = AsyncMock()
        with (
            patch.object(
                db, "get_active_ml_close_month_work", AsyncMock(return_value=None)
            ),
            patch.object(db, "claim_ml_close_action", claimer),
            patch.object(db, "discard_ml_close_action", discarder),
            patch.object(db, "cancel_unified_report_generation", canceler),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(side_effect=[REPORT_HASH, "changed-before-cancel"]),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", audit_mock),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action(
                {
                    "value": {
                        "action": "ml_profit_ops_reject",
                        "period": PERIOD,
                        "report_hash": REPORT_HASH,
                    },
                    "message_id": "om-current",
                    "operator_name": "梁俊辉",
                    "operator_id": ml_close.ML_CLOSE_OPS_APPROVER_OPEN_ID,
                    "patch_message": False,
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertIn("退回前报表版本已变化", result["reason"])
        claimer.assert_awaited_once()
        discarder.assert_awaited_once()
        canceler.assert_not_awaited()
        writer.assert_not_awaited()
        audit_mock.assert_not_awaited()

    async def test_reject_rechecks_hash_before_final_status_write(self):
        status = {
            "record_id": "rec-status",
            "fields": {
                "状态": "待运营确认",
                "最后卡片 message_id": "om-current",
                "最后结果JSON": "{}",
            },
        }
        summary = {
            "status": "ok",
            "period": PERIOD,
            "month": "2026-08",
            "state": "待运营确认",
            "next_card": "ops_operating",
            "last_error": "",
            "report_hash": REPORT_HASH,
            "ab_verified": False,
            "operating_ready": True,
        }
        writer = AsyncMock()
        canceler = AsyncMock()
        with (
            patch.object(
                db, "get_active_ml_close_month_work", AsyncMock(return_value=None)
            ),
            patch.object(
                db,
                "claim_ml_close_action",
                AsyncMock(return_value={"claimed": True, "status": "processing"}),
            ),
            patch.object(db, "fail_ml_close_action", AsyncMock()),
            patch.object(db, "cancel_unified_report_generation", canceler),
            patch.object(db, "ml_close_action_finalization_guard", ready_action_guard),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(side_effect=[REPORT_HASH, REPORT_HASH, "changed-before-write"]),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=summary)),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action(
                {
                    "action": "ml_profit_ops_reject",
                    "period": PERIOD,
                    "report_hash": REPORT_HASH,
                    "message_id": "om-current",
                    "operator_name": "梁俊辉",
                    "operator_id": ml_close.ML_CLOSE_OPS_APPROVER_OPEN_ID,
                    "patch_message": False,
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertIn("退回写入前报表版本已变化", result["reason"])
        self.assertGreater(canceler.await_count, 0)
        writer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
