import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import db, main, ml_close, unified_report


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


class MlUnauthorizedConfirmationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _status(self, *, restored: bool = False) -> dict:
        fields = {
            "周期": "month_2026-08",
            "状态": "待运营确认" if restored else "运营已确认",
            "运营确认人": "" if restored else ml_close.IDENTITY_RECOVERY_WRONG_OPERATOR,
            "最后卡片 message_id": ml_close.IDENTITY_RECOVERY_WRONG_FINANCE_CARD,
            "最后按钮动作Key": "old-action-key",
            "最后按钮动作时间": 1789376638046,
            "最后结果JSON": json.dumps(
                {
                    "report_hash": ml_close.IDENTITY_RECOVERY_REPORT_HASH,
                    "review_source_hash": ml_close.IDENTITY_RECOVERY_REPORT_HASH,
                    "review_report_url": ml_close.IDENTITY_RECOVERY_REVIEW_URL,
                    "ab_verified": False,
                },
                ensure_ascii=False,
            ),
        }
        if not restored:
            fields["运营确认时间"] = 1789376638046
        return {
            "record_id": ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID,
            "table_id": ml_close.IDENTITY_RECOVERY_STATUS_TABLE_ID,
            "fields": fields,
        }

    @staticmethod
    def _status_record(status: dict) -> dict:
        return {
            "data": {
                "record": {
                    "record_id": status["record_id"],
                    "fields": status["fields"],
                }
            }
        }

    @staticmethod
    def _revoked_card() -> dict:
        return {
            "data": {
                "items": [
                    {
                        "updated": True,
                        "deleted": False,
                        "body": {
                            "content": json.dumps(
                                {
                                    "header": {
                                        "title": {
                                            "tag": "plain_text",
                                            "content": "美客多财务卡已撤销",
                                        }
                                    },
                                    "elements": [
                                        {
                                            "tag": "markdown",
                                            "content": "当前版本不可确认，等待负责人重走。",
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ]
            }
        }

    @staticmethod
    def _planned_update() -> dict:
        return {
            "fields": {
                "状态": "待运营确认",
                "运营确认人": "",
                "运营确认时间": None,
            }
        }

    def _preflight_hash(self, status: dict, card: dict | None = None) -> str:
        card = card or self._revoked_card()
        card_document = json.loads(card["data"]["items"][0]["body"]["content"])
        return ml_close._identity_recovery_preflight_hash(
            status["fields"],
            ml_close.IDENTITY_RECOVERY_REPORT_HASH,
            card_document,
            ml_close.IDENTITY_RECOVERY_REVIEW_MARKER,
            ml_close.IDENTITY_RECOVERY_COMPANY_MONTH,
            ml_close.IDENTITY_RECOVERY_COMPANY_V3_URL,
            self._planned_update(),
        )

    async def test_recovery_uses_lock_and_writes_exactly_three_fields(self):
        class LockProbe:
            entered = False
            exited = False

            async def __aenter__(self):
                self.entered = True
                return self

            async def __aexit__(self, exc_type, exc, tb):
                self.exited = True

        before = self._status()
        after = self._status(restored=True)
        lock = LockProbe()
        token = AsyncMock(
            side_effect=[
                "base-token",
                "card-token",
                "base-token",
                "card-token",
                "base-token",
                "card-token",
            ]
        )
        current_hash = AsyncMock(return_value=ml_close.IDENTITY_RECOVERY_REPORT_HASH)
        ad_failures = AsyncMock(return_value=[])
        status_reads = 0
        applied = False

        async def fs_json(method, url, tok, payload=None, **kwargs):
            nonlocal applied, status_reads
            if (
                method == "GET"
                and ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID in url
            ):
                status_reads += 1
                return self._status_record(after if applied else before)
            if method == "GET" and "/im/v1/messages/" in url:
                return self._revoked_card()
            if method == "GET" and "recvuoIDRcdbvh" in url:
                return {
                    "data": {
                        "record": {
                            "fields": {
                                "日期": ml_close.IDENTITY_RECOVERY_COMPANY_MONTH,
                                "美客多毛利报表": {
                                    "link": ml_close.IDENTITY_RECOVERY_COMPANY_V3_URL
                                }
                            }
                        }
                    }
                }
            if method == "PUT" and "/records/" in url:
                applied = True
                return {"code": 0}
            self.fail(f"unexpected Feishu request: {method} {url}")

        feishu = AsyncMock(side_effect=fs_json)
        with (
            patch.object(
                ml_close, "_status_mutation_lock", return_value=lock
            ) as lock_factory,
            patch.object(ml_close, "_tenant_token", token),
            patch.object(
                ml_close,
                "_get_status",
                AsyncMock(side_effect=AssertionError("generic status read is forbidden")),
            ),
            patch.object(ml_close, "_current_report_hash", current_hash),
            patch.object(ml_close, "_open_ad_failures", ad_failures),
            patch.object(ml_close, "_fs_json", feishu),
            patch.object(
                unified_report,
                "_spreadsheet_meta",
                AsyncMock(return_value=[{"title": "检查", "sheetId": "check"}]),
            ),
            patch.object(
                unified_report,
                "_read_range",
                AsyncMock(return_value=[[ml_close.IDENTITY_RECOVERY_REVIEW_MARKER]]),
            ),
        ):
            preflight = await ml_close.recover_identity_incident(
                ml_close.IDENTITY_RECOVERY_INCIDENT_ID
            )
            self.assertEqual("preflight_ok", preflight["status"])
            self.assertFalse(preflight["commit"])
            self.assertEqual(64, len(preflight["preflight_hash"]))
            self.assertFalse(
                any(
                    call.args[0] == "PUT"
                    for call in feishu.await_args_list
                    if call.args
                )
            )
            with self.assertRaisesRegex(ValueError, "预演指纹缺失或已变化"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID,
                    commit=True,
                    expected_preflight_hash="0" * 64,
                )
            self.assertFalse(
                any(
                    call.args[0] == "PUT"
                    for call in feishu.await_args_list
                    if call.args
                )
            )
            result = await ml_close.recover_identity_incident(
                ml_close.IDENTITY_RECOVERY_INCIDENT_ID,
                commit=True,
                expected_preflight_hash=preflight["preflight_hash"],
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual("待运营确认", result["state"])
        self.assertEqual(3, lock_factory.call_count)
        self.assertTrue(
            all(
                call.args == (ml_close.IDENTITY_RECOVERY_PERIOD,)
                for call in lock_factory.call_args_list
            )
        )
        self.assertTrue(lock.entered)
        self.assertTrue(lock.exited)
        put_calls = [
            call
            for call in feishu.await_args_list
            if call.args[0] == "PUT" and "/records/" in call.args[1]
        ]
        self.assertEqual(1, len(put_calls))
        self.assertEqual(
            {
                "fields": {
                    "状态": "待运营确认",
                    "运营确认人": "",
                    "运营确认时间": None,
                }
            },
            put_calls[0].args[3],
        )
        self.assertEqual(4, status_reads)
        self.assertEqual(
            {
                "ready_for_finance": False,
                "ready_for_management": False,
                "ready_for_commission": False,
                "ready_for_final_reconciliation": False,
            },
            result["release_flags"],
        )
        self.assertFalse(
            any(
                call.args[0] in {"POST", "PATCH", "DELETE"}
                for call in feishu.await_args_list
                if call.args
            )
        )

    async def test_recovery_rejects_wrong_incident_before_any_io(self):
        token = AsyncMock()
        with patch.object(ml_close, "_tenant_token", token):
            with self.assertRaisesRegex(ValueError, "恢复编号"):
                await ml_close.recover_identity_incident("wrong-incident")
        token.assert_not_awaited()

    async def test_recovery_rejects_wrong_remote_service_identity_before_io(self):
        for attribute, bad_value in (
            ("CARD_APP_ID", "cli_wrong_card_app"),
            ("APP_TOKEN", "wrong_base_app_token"),
            ("REPORT_TABLE_ID", "wrong_report_table"),
            ("STATUS_TABLE_ID_ENV", "wrong_status_table"),
        ):
            with self.subTest(attribute=attribute):
                token = AsyncMock()
                with (
                    patch.object(ml_close, attribute, bad_value),
                    patch.object(ml_close, "_tenant_token", token),
                ):
                    with self.assertRaisesRegex(ValueError, "服务身份或数据源配置"):
                        await ml_close.recover_identity_incident(
                            ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                        )
                token.assert_not_awaited()

        token = AsyncMock()
        with (
            patch.dict(os.environ, {"FEISHU_APP_ID": "cli_wrong_base_app"}),
            patch.object(ml_close, "_tenant_token", token),
        ):
            with self.assertRaisesRegex(ValueError, "服务身份或数据源配置"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                )
        token.assert_not_awaited()

    async def test_recovery_route_returns_non_2xx_for_safety_rejections(self):
        for exc, expected_status in (
            (ValueError("预演指纹已变化"), 409),
            (RuntimeError("写后回读不确定"), 500),
        ):
            with self.subTest(expected_status=expected_status):
                recovery = AsyncMock(side_effect=exc)
                with patch.object(ml_close, "recover_identity_incident", recovery):
                    with self.assertRaises(HTTPException) as raised:
                        await main.ml_close_recover_identity_incident(
                            ml_close.IDENTITY_RECOVERY_INCIDENT_ID,
                            commit=True,
                            expected_preflight_hash="0" * 64,
                        )
                self.assertEqual(expected_status, raised.exception.status_code)
                self.assertEqual("error", raised.exception.detail["status"])
                recovery.assert_awaited_once()

    async def test_recovery_rejects_changed_state_before_put(self):
        changed = self._status()
        changed["fields"]["状态"] = "财务已确认暂结"
        feishu = AsyncMock(return_value=self._status_record(changed))
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(
                ml_close,
                "_get_status",
                AsyncMock(side_effect=AssertionError("generic status read is forbidden")),
            ),
            patch.object(ml_close, "_fs_json", feishu),
        ):
            with self.assertRaisesRegex(ValueError, "状态已变化"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                )
        self.assertEqual(1, feishu.await_count)
        self.assertEqual("GET", feishu.await_args.args[0])
        self.assertIn(
            ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID,
            feishu.await_args.args[1],
        )

    async def test_recovery_rejects_changed_period_before_put(self):
        changed = self._status()
        changed["fields"]["周期"] = "month_2026-09"
        feishu = AsyncMock(return_value=self._status_record(changed))
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_fs_json", feishu),
        ):
            with self.assertRaisesRegex(ValueError, "记录周期已变化"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                )
        self.assertEqual(1, feishu.await_count)
        self.assertEqual("GET", feishu.await_args.args[0])

    async def test_recovery_rejects_repeated_call_before_put(self):
        restored = self._status(restored=True)
        feishu = AsyncMock(return_value=self._status_record(restored))
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_fs_json", feishu),
        ):
            with self.assertRaisesRegex(ValueError, "状态已变化"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                )
        self.assertEqual(1, feishu.await_count)
        self.assertEqual("GET", feishu.await_args.args[0])

    async def test_recovery_requires_revoked_finance_card_before_put(self):
        for unsafe_marker in (
            "ml_profit_finance_operating_confirm",
            "ml_profit_finance_reject",
            "退回运营复核",
        ):
            with self.subTest(unsafe_marker=unsafe_marker):
                before = self._status()
                active_card = self._revoked_card()
                active_card["data"]["items"][0]["body"]["content"] += (
                    " " + unsafe_marker
                )

                async def fs_json(method, url, tok, payload=None, **kwargs):
                    if (
                        method == "GET"
                        and ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID in url
                    ):
                        return self._status_record(before)
                    if method == "GET" and "/im/v1/messages/" in url:
                        return active_card
                    self.fail(f"unexpected Feishu request: {method} {url}")

                feishu = AsyncMock(side_effect=fs_json)
                with (
                    patch.object(
                        ml_close,
                        "_tenant_token",
                        AsyncMock(side_effect=["base-token", "card-token"]),
                    ),
                    patch.object(
                        ml_close,
                        "_get_status",
                        AsyncMock(
                            side_effect=AssertionError(
                                "generic status read is forbidden"
                            )
                        ),
                    ),
                    patch.object(
                        ml_close,
                        "_current_report_hash",
                        AsyncMock(
                            return_value=ml_close.IDENTITY_RECOVERY_REPORT_HASH
                        ),
                    ),
                    patch.object(
                        ml_close, "_open_ad_failures", AsyncMock(return_value=[])
                    ),
                    patch.object(ml_close, "_fs_json", feishu),
                ):
                    with self.assertRaisesRegex(ValueError, "财务卡仍可操作"):
                        await ml_close.recover_identity_incident(
                            ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                        )
                self.assertFalse(
                    any(
                        call.args[0] == "PUT"
                        for call in feishu.await_args_list
                        if call.args
                    )
                )

    async def test_recovery_rejects_changed_company_month_before_put(self):
        before = self._status()

        async def fs_json(method, url, tok, payload=None, **kwargs):
            if (
                method == "GET"
                and ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID in url
            ):
                return self._status_record(before)
            if method == "GET" and "/im/v1/messages/" in url:
                return self._revoked_card()
            if method == "GET" and ml_close.IDENTITY_RECOVERY_COMPANY_RECORD_ID in url:
                return {
                    "data": {
                        "record": {
                            "fields": {
                                "日期": "2026/09",
                                "美客多毛利报表": {
                                    "link": ml_close.IDENTITY_RECOVERY_COMPANY_V3_URL
                                },
                            }
                        }
                    }
                }
            self.fail(f"unexpected Feishu request: {method} {url}")

        feishu = AsyncMock(side_effect=fs_json)
        with (
            patch.object(
                ml_close,
                "_tenant_token",
                AsyncMock(side_effect=["base-token", "card-token"]),
            ),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value=ml_close.IDENTITY_RECOVERY_REPORT_HASH),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_fs_json", feishu),
            patch.object(
                unified_report,
                "_spreadsheet_meta",
                AsyncMock(return_value=[{"title": "检查", "sheetId": "check"}]),
            ),
            patch.object(
                unified_report,
                "_read_range",
                AsyncMock(return_value=[[ml_close.IDENTITY_RECOVERY_REVIEW_MARKER]]),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "公司汇总月份已变化"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                )
        self.assertFalse(
            any(
                call.args[0] == "PUT"
                for call in feishu.await_args_list
                if call.args
            )
        )

    async def test_recovery_accepts_applied_write_after_timeout_readback(self):
        before = self._status()
        after = self._status(restored=True)
        status_reads = 0

        async def fs_json(method, url, tok, payload=None, **kwargs):
            nonlocal status_reads
            if (
                method == "GET"
                and ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID in url
            ):
                status_reads += 1
                return self._status_record(before if status_reads == 1 else after)
            if method == "GET" and "/im/v1/messages/" in url:
                return self._revoked_card()
            if method == "GET" and ml_close.IDENTITY_RECOVERY_COMPANY_RECORD_ID in url:
                return {
                    "data": {
                        "record": {
                            "fields": {
                                "日期": ml_close.IDENTITY_RECOVERY_COMPANY_MONTH,
                                "美客多毛利报表": {
                                    "link": ml_close.IDENTITY_RECOVERY_COMPANY_V3_URL
                                },
                            }
                        }
                    }
                }
            if method == "PUT":
                raise TimeoutError("response lost after apply")
            self.fail(f"unexpected Feishu request: {method} {url}")

        with (
            patch.object(
                ml_close,
                "_tenant_token",
                AsyncMock(side_effect=["base-token", "card-token"]),
            ),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value=ml_close.IDENTITY_RECOVERY_REPORT_HASH),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_fs_json", AsyncMock(side_effect=fs_json)),
            patch.object(
                unified_report,
                "_spreadsheet_meta",
                AsyncMock(return_value=[{"title": "检查", "sheetId": "check"}]),
            ),
            patch.object(
                unified_report,
                "_read_range",
                AsyncMock(return_value=[[ml_close.IDENTITY_RECOVERY_REVIEW_MARKER]]),
            ),
        ):
            result = await ml_close.recover_identity_incident(
                ml_close.IDENTITY_RECOVERY_INCIDENT_ID,
                commit=True,
                expected_preflight_hash=self._preflight_hash(before),
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual("applied_after_uncertain_response", result["put_outcome"])
        self.assertTrue(result["release_flags_closed"])

    async def test_recovery_stops_when_timeout_readback_shows_not_applied(self):
        before = self._status()

        async def fs_json(method, url, tok, payload=None, **kwargs):
            if (
                method == "GET"
                and ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID in url
            ):
                return self._status_record(before)
            if method == "GET" and "/im/v1/messages/" in url:
                return self._revoked_card()
            if method == "GET" and ml_close.IDENTITY_RECOVERY_COMPANY_RECORD_ID in url:
                return {
                    "data": {
                        "record": {
                            "fields": {
                                "日期": ml_close.IDENTITY_RECOVERY_COMPANY_MONTH,
                                "美客多毛利报表": {
                                    "link": ml_close.IDENTITY_RECOVERY_COMPANY_V3_URL
                                },
                            }
                        }
                    }
                }
            if method == "PUT":
                raise TimeoutError("request timed out")
            self.fail(f"unexpected Feishu request: {method} {url}")

        with (
            patch.object(
                ml_close,
                "_tenant_token",
                AsyncMock(side_effect=["base-token", "card-token"]),
            ),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value=ml_close.IDENTITY_RECOVERY_REPORT_HASH),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_fs_json", AsyncMock(side_effect=fs_json)),
            patch.object(ml_close.asyncio, "sleep", AsyncMock()),
            patch.object(
                unified_report,
                "_spreadsheet_meta",
                AsyncMock(return_value=[{"title": "检查", "sheetId": "check"}]),
            ),
            patch.object(
                unified_report,
                "_read_range",
                AsyncMock(return_value=[[ml_close.IDENTITY_RECOVERY_REVIEW_MARKER]]),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "写入未生效"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID,
                    commit=True,
                    expected_preflight_hash=self._preflight_hash(before),
                )

    async def test_recovery_rejects_unknown_interactive_control(self):
        before = self._status()
        card = self._revoked_card()
        document = json.loads(card["data"]["items"][0]["body"]["content"])
        document["elements"].append(
            {"tag": "button", "text": {"tag": "plain_text", "content": "打开"}}
        )
        card["data"]["items"][0]["body"]["content"] = json.dumps(
            document, ensure_ascii=False
        )

        async def fs_json(method, url, tok, payload=None, **kwargs):
            if (
                method == "GET"
                and ml_close.IDENTITY_RECOVERY_STATUS_RECORD_ID in url
            ):
                return self._status_record(before)
            if method == "GET" and "/im/v1/messages/" in url:
                return card
            self.fail(f"unexpected Feishu request: {method} {url}")

        feishu = AsyncMock(side_effect=fs_json)
        with (
            patch.object(
                ml_close,
                "_tenant_token",
                AsyncMock(side_effect=["base-token", "card-token"]),
            ),
            patch.object(
                ml_close,
                "_current_report_hash",
                AsyncMock(return_value=ml_close.IDENTITY_RECOVERY_REPORT_HASH),
            ),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "_fs_json", feishu),
        ):
            with self.assertRaisesRegex(ValueError, "仍含可交互控件"):
                await ml_close.recover_identity_incident(
                    ml_close.IDENTITY_RECOVERY_INCIDENT_ID
                )
        self.assertFalse(
            any(
                call.args[0] == "PUT"
                for call in feishu.await_args_list
                if call.args
            )
        )


if __name__ == "__main__":
    unittest.main()
