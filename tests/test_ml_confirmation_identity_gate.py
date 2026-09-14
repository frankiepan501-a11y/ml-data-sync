import unittest
from unittest.mock import AsyncMock, patch

from app import db, ml_close, unified_report


class MlConfirmationIdentityGateTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_rejected_before_business_side_effects(
        self,
        *,
        action: str,
        operator_id: str,
        expected_name: str,
        ops_approver_id: str = "ou_1ad46c8394e00b1b3fd41bcb19bc1dba",
        finance_approver_id: str = "ou_2ced41d585239cb0e8aebd9b5b7b28f0",
        operator_name: str = "非指定人员",
    ) -> None:
        active_work = AsyncMock(return_value=None)
        token = AsyncMock(return_value="token")
        status = AsyncMock(return_value={"record_id": "status-1", "fields": {"状态": "退回重算"}})
        claim = AsyncMock(return_value={"claimed": True, "status": "processing"})
        writer = AsyncMock()
        patcher = AsyncMock(return_value={})
        sender = AsyncMock()
        review = AsyncMock()
        generator = AsyncMock()

        with (
            patch.object(ml_close, "ML_CLOSE_OPS_APPROVER_OPEN_ID", ops_approver_id, create=True),
            patch.object(
                ml_close,
                "ML_CLOSE_FINANCE_APPROVER_OPEN_ID",
                finance_approver_id,
                create=True,
            ),
            patch.object(db, "get_active_ml_close_month_work", active_work),
            patch.object(db, "claim_ml_close_action", claim),
            patch.object(ml_close, "_tenant_token", token),
            patch.object(ml_close, "_get_status", status),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "patch_or_fallback", patcher),
            patch.object(ml_close, "send_card", sender),
            patch.object(ml_close, "_prepare_finance_review", review),
            patch.object(unified_report, "generate", generator),
        ):
            result = await ml_close.confirm_action(
                {
                    "action": action,
                    "period": "month_2026-08",
                    "message_id": "om-current-valid-card",
                    "operator_id": operator_id,
                    "operator_name": operator_name,
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("未变更", result["state"])
        self.assertIn("身份校验未通过", result["reason"])
        self.assertIn(expected_name, result["reason"])
        self.assertEqual({}, result["feedback"])
        active_work.assert_not_awaited()
        token.assert_not_awaited()
        status.assert_not_awaited()
        claim.assert_not_awaited()
        writer.assert_not_awaited()
        patcher.assert_not_awaited()
        sender.assert_not_awaited()
        review.assert_not_awaited()
        generator.assert_not_awaited()

    async def test_all_four_confirmation_actions_reject_wrong_app3_operator(self):
        cases = (
            ("ml_profit_ops_confirm", "梁俊辉"),
            ("ml_profit_ops_waive_gap", "梁俊辉"),
            ("ml_profit_finance_operating_confirm", "林纯子"),
            ("ml_profit_finance_confirm", "林纯子"),
        )
        for action, expected_name in cases:
            with self.subTest(action=action):
                await self._assert_rejected_before_business_side_effects(
                    action=action,
                    operator_id="ou_8bdf206fafc9086a3e6f2742bb54ccb3",
                    expected_name=expected_name,
                    operator_name=expected_name,
                )

    async def test_same_name_from_app1_namespace_does_not_bypass_gate(self):
        await self._assert_rejected_before_business_side_effects(
            action="ml_profit_ops_confirm",
            operator_id="ou_b9dd2272e72908fe68964d7bba53109f",
            expected_name="梁俊辉",
            operator_name="梁俊辉",
        )

    async def test_missing_operator_id_does_not_trust_display_name(self):
        await self._assert_rejected_before_business_side_effects(
            action="ml_profit_finance_operating_confirm",
            operator_id="",
            expected_name="林纯子",
            operator_name="林纯子",
        )

    async def test_empty_approver_configuration_fails_closed(self):
        await self._assert_rejected_before_business_side_effects(
            action="ml_profit_ops_confirm",
            operator_id="ou_1ad46c8394e00b1b3fd41bcb19bc1dba",
            expected_name="梁俊辉",
            ops_approver_id="",
            operator_name="梁俊辉",
        )

    async def test_reject_and_recalculate_actions_remain_outside_identity_gate(self):
        for action in (
            "ml_profit_ops_reject",
            "ml_profit_finance_reject",
            "ml_profit_recalc_cost",
        ):
            with self.subTest(action=action):
                token = AsyncMock(side_effect=RuntimeError("sentinel after identity gate"))
                status = AsyncMock()
                patcher = AsyncMock(return_value={})
                with (
                    patch.object(ml_close, "_tenant_token", token),
                    patch.object(ml_close, "_get_status", status),
                    patch.object(ml_close, "patch_or_fallback", patcher),
                ):
                    result = await ml_close.confirm_action(
                        {
                            "action": action,
                            "period": "month_2026-08",
                            "message_id": "om-current-valid-card",
                            "operator_id": "ou_any_existing_member",
                        }
                    )

                self.assertNotIn("身份校验未通过", result.get("reason", ""))
                token.assert_awaited_once()
                status.assert_not_awaited()
                patcher.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
