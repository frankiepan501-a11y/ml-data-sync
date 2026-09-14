import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from app import db, ml_close, unified_report


PERIOD = "month_2026-08"


@asynccontextmanager
async def _ready_action_guard(*args, **kwargs):
    yield True


def _report_row():
    return {
        "record_id": "rec-report",
        "fields": {
            "周期": PERIOD,
            "店铺": "ML 巴西本土店 AIRSOFT COMERCIAL",
            "SKU": "TZ07",
            "全额毛利(RMB)": 0,
        },
    }


def _stale_operating_status():
    return {
        "record_id": "rec-status",
        "fields": {
            "状态": "财务已确认暂结",
            "最后卡片 message_id": "om-current",
            "最后结果JSON": json.dumps(
                {
                    "report_hash": "legacy-v3-hash",
                    "operating_close_confirmed": True,
                    "operating_report_hash": "legacy-v3-hash",
                    "operating_content_hash": "legacy-v3-content",
                    "operating_report_url": "https://example.test/wiki/v3",
                    "operating_report_sheet_url": "https://example.test/sheets/v3",
                    "operating_closed_at": 1,
                    "operating_closed_by": "财务",
                    "review_source_hash": "legacy-v3-hash",
                    "review_report_url": "https://example.test/sheets/v3-review",
                },
                ensure_ascii=False,
            ),
        },
    }


def _stale_final_status():
    status = _stale_operating_status()
    status["fields"]["状态"] = "财务已确认终稿"
    result = json.loads(status["fields"]["最后结果JSON"])
    result.update(
        {
            "ab_verified": True,
            "ab_report_hash": "legacy-v3-hash",
            "final_report_url": "https://example.test/wiki/v3-final",
        }
    )
    status["fields"]["最后结果JSON"] = json.dumps(result, ensure_ascii=False)
    return status


class ReportFormatRevisionTests(unittest.IsolatedAsyncioTestCase):
    def test_v4_revision_is_part_of_identity_title_and_source_approval_hash(self):
        rows = [_report_row()]
        with patch.object(
            unified_report, "REPORT_FORMAT_REVISION", "V3", create=True
        ):
            v3_hash = unified_report.source_report_hash(rows, PERIOD)
        with patch.object(
            unified_report, "REPORT_FORMAT_REVISION", "V4", create=True
        ):
            v4_hash = unified_report.source_report_hash(rows, PERIOD)
            close_hash = ml_close._report_content_hash(rows)

        self.assertNotEqual(v3_hash, v4_hash)
        self.assertEqual(v4_hash, close_hash)
        self.assertEqual(f"{PERIOD}::V4", unified_report._report_identity(PERIOD, "final"))
        self.assertEqual(
            f"{PERIOD}::operating::V4",
            unified_report._report_identity(PERIOD, "operating"),
        )
        self.assertEqual(
            "美客多毛利报表-2026-08-经营暂结-V4",
            unified_report._report_title("2026-08", "operating"),
        )

    async def test_audit_resets_stale_operating_confirmation_to_ops_review(self):
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(
                ml_close, "_get_status", AsyncMock(return_value=_stale_operating_status())
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=[_report_row()])),
            patch.object(ml_close, "_report_content_hash", return_value="current-v4-hash"),
        ):
            result = await ml_close.audit(period=PERIOD, run_cost_preview=False)

        self.assertEqual("待运营确认", result["state"])
        self.assertEqual("ops_operating", result["next_card"])
        self.assertFalse(result["operating_close_confirmed"])
        self.assertEqual("", result["operating_report_hash"])
        self.assertEqual("", result["operating_report_url"])

    async def test_commit_does_not_copy_stale_operating_approval_into_last_result(self):
        writer = AsyncMock(return_value={"record_id": "rec-status"})
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(
                ml_close, "_get_status", AsyncMock(return_value=_stale_operating_status())
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=[_report_row()])),
            patch.object(ml_close, "_report_content_hash", return_value="current-v4-hash"),
            patch.object(ml_close, "_upsert_status", writer),
        ):
            result = await ml_close.audit(
                period=PERIOD, commit=True, run_cost_preview=False
            )

        written = writer.await_args.args[1]
        stored = json.loads(written["最后结果JSON"])
        self.assertEqual("待运营确认", written["状态"])
        self.assertEqual("ops_operating", stored["next_card"])
        self.assertFalse(stored.get("operating_close_confirmed"))
        self.assertFalse(stored.get("operating_report_hash"))
        self.assertNotIn("operating_report_url", stored)
        self.assertNotIn("review_source_hash", stored)
        self.assertNotIn("review_report_url", stored)
        self.assertEqual("待运营确认", result["state"])

    async def test_stale_finance_callback_is_not_deduped_as_current_v4(self):
        status = _stale_operating_status()
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
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=[_report_row()])),
            patch.object(ml_close, "_report_content_hash", return_value="current-v4-hash"),
            patch.object(
                ml_close,
                "audit",
                AsyncMock(
                    return_value={
                        "status": "ok",
                        "period": PERIOD,
                        "month": "2026-08",
                        "state": "待运营确认",
                        "next_card": "ops_operating",
                        "last_error": "",
                        "report_hash": "current-v4-hash",
                        "ab_verified": False,
                    }
                ),
            ),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action(
                {
                    "action": "ml_profit_finance_operating_confirm",
                    "period": PERIOD,
                    "message_id": "om-current",
                    "operator_name": "财务",
                    "patch_message": False,
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertNotIn("deduped", result)
        self.assertIn("审核版缺失", result["reason"])

    async def test_current_v4_ops_confirmation_continues_from_stale_v3_final_state(self):
        status = _stale_final_status()
        summary = {
            "status": "ok",
            "period": PERIOD,
            "month": "2026-08",
            "state": "待运营确认",
            "next_card": "ops_operating",
            "last_error": "",
            "report_hash": "current-v4-hash",
            "ab_verified": False,
            "operating_ready": True,
        }
        writer = AsyncMock(return_value={"record_id": "rec-status"})
        sender = AsyncMock(
            return_value={"data": {"message_id": "om-v4-finance-review"}}
        )
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
            patch.object(db, "ml_close_action_finalization_guard", _ready_action_guard),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value="current-v4-hash"),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=summary)),
            patch.object(
                ml_close,
                "_prepare_finance_review",
                AsyncMock(
                    return_value={
                        **summary,
                        "report_url": "https://example.test/sheets/v4-review",
                        "review_source_hash": "current-v4-hash",
                    }
                ),
            ),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "send_card", sender),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action(
                {
                    "action": "ml_profit_ops_confirm",
                    "period": PERIOD,
                    "message_id": "om-current",
                    "operator_name": "运营",
                    "patch_message": False,
                }
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual("运营已确认", result["state"])
        self.assertEqual("finance_operating", result["next_kind"])
        self.assertTrue(
            any(call.args[1].get("状态") == "运营已确认" for call in writer.await_args_list)
        )
        sender.assert_awaited_once()

    async def test_ops_confirmation_fails_closed_if_final_state_loses_version_during_click(self):
        stale_status = _stale_final_status()
        unversioned_final = {
            "record_id": "rec-status",
            "fields": {
                "状态": "财务已确认终稿",
                "最后卡片 message_id": "om-current",
            },
        }
        status_reads = 0

        async def get_status(*args, **kwargs):
            nonlocal status_reads
            status_reads += 1
            return stale_status if status_reads <= 3 else unversioned_final

        summary = {
            "status": "ok",
            "period": PERIOD,
            "month": "2026-08",
            "state": "待运营确认",
            "next_card": "ops_operating",
            "last_error": "",
            "report_hash": "current-v4-hash",
            "ab_verified": False,
            "operating_ready": True,
        }
        writer = AsyncMock(return_value={"record_id": "rec-status"})
        sender = AsyncMock(
            return_value={"data": {"message_id": "om-v4-finance-review"}}
        )
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
            patch.object(db, "ml_close_action_finalization_guard", _ready_action_guard),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", side_effect=get_status),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value="current-v4-hash"),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=summary)),
            patch.object(
                ml_close,
                "_prepare_finance_review",
                AsyncMock(
                    return_value={
                        **summary,
                        "report_url": "https://example.test/sheets/v4-review",
                        "review_source_hash": "current-v4-hash",
                    }
                ),
            ),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "send_card", sender),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action(
                {
                    "action": "ml_profit_ops_confirm",
                    "period": PERIOD,
                    "message_id": "om-current",
                    "operator_name": "运营",
                    "patch_message": False,
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertIn("财务确认终稿", result["reason"])
        self.assertFalse(
            any(call.args[1].get("状态") == "运营已确认" for call in writer.await_args_list)
        )
        sender.assert_not_awaited()

    async def test_card_endpoint_emits_new_ops_card_for_stale_v3_state(self):
        status = _stale_operating_status()
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=[_report_row()])),
            patch.object(ml_close, "_report_content_hash", return_value="current-v4-hash"),
            patch.object(ml_close.meitong_cost, "run", return_value={"status": "ok"}),
        ):
            result = await ml_close.card_endpoint(period=PERIOD)

        self.assertEqual("ok", result["status"])
        self.assertEqual("ops_operating", result["kind"])
        self.assertEqual("待运营确认", result["summary"]["state"])

    async def test_card_endpoint_does_not_early_skip_stale_v3_final_state(self):
        status = _stale_final_status()
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=[_report_row()])),
            patch.object(ml_close, "_report_content_hash", return_value="current-v4-hash"),
            patch.object(ml_close.meitong_cost, "run", return_value={"status": "ok"}),
        ):
            result = await ml_close.card_endpoint(period=PERIOD)

        self.assertEqual("ok", result["status"])
        self.assertEqual("ops_operating", result["kind"])
        self.assertEqual("待运营确认", result["summary"]["state"])

    async def test_status_endpoint_fails_closed_for_stale_v3_final_state(self):
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(
                ml_close, "_get_status", AsyncMock(return_value=_stale_final_status())
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value="current-v4-hash"),
            ),
        ):
            result = await ml_close.status_endpoint(period=PERIOD)

        self.assertEqual("待运营确认", result["state"])
        self.assertEqual("current-v4-hash", result["report_hash"])
        self.assertFalse(result["ab_verified"])
        self.assertFalse(result["operating_snapshot_current"])
        self.assertFalse(result["ready_for_finance"])
        self.assertFalse(result["ready_for_management"])
        self.assertFalse(result["ready_for_commission"])
        self.assertFalse(result["ready_for_final_reconciliation"])

    async def test_status_endpoint_keeps_current_v4_operating_snapshot_ready(self):
        status = _stale_operating_status()
        current = json.loads(status["fields"]["最后结果JSON"])
        current.update(
            {
                "report_hash": "current-v4-hash",
                "operating_report_hash": "current-v4-hash",
            }
        )
        status["fields"]["最后结果JSON"] = json.dumps(current, ensure_ascii=False)
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value="current-v4-hash"),
            ),
        ):
            result = await ml_close.status_endpoint(period=PERIOD)

        self.assertEqual("财务已确认暂结", result["state"])
        self.assertTrue(result["operating_snapshot_current"])
        self.assertTrue(result["ready_for_finance"])
        self.assertTrue(result["ready_for_management"])
        self.assertTrue(result["ready_for_commission"])

    async def test_reject_cancels_v4_and_legacy_generation_keys(self):
        cancel = AsyncMock()
        status = {
            "record_id": "rec-status",
            "fields": {
                "状态": "待运营确认",
                "最后卡片 message_id": "om-current",
                "最后结果JSON": '{"report_hash":"current-v4-hash"}',
            },
        }
        with (
            patch.object(
                db, "get_active_ml_close_month_work", AsyncMock(return_value=None)
            ),
            patch.object(
                db,
                "claim_ml_close_action",
                AsyncMock(return_value={"claimed": True, "status": "processing"}),
            ),
            patch.object(db, "cancel_unified_report_generation", cancel),
            patch.object(db, "fail_ml_close_action", AsyncMock()),
            patch.object(db, "discard_ml_close_action", AsyncMock()),
            patch.object(db, "ml_close_action_finalization_guard", _ready_action_guard),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_upsert_status", AsyncMock(return_value={})),
            patch.object(
                ml_close,
                "audit",
                AsyncMock(
                    return_value={
                        "status": "ok",
                        "period": PERIOD,
                        "month": "2026-08",
                        "state": "待运营确认",
                        "next_card": "ops_operating",
                        "last_error": "",
                        "report_hash": "current-v4-hash",
                    }
                ),
            ),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action(
                {
                    "action": "ml_profit_ops_reject",
                    "period": PERIOD,
                    "message_id": "om-current",
                    "operator_name": "运营",
                    "patch_message": False,
                }
            )

        self.assertEqual("退回重算", result["state"])
        cancelled = [call.args[0] for call in cancel.await_args_list]
        self.assertEqual(
            {
                f"{PERIOD}::V4",
                f"{PERIOD}::operating::V4",
                f"{PERIOD}::review::V4",
                PERIOD,
                f"{PERIOD}::operating",
                f"{PERIOD}::review",
            },
            set(cancelled),
        )


if __name__ == "__main__":
    unittest.main()
