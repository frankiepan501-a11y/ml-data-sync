import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import advertising, db, lingxing, main, ml_close


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake response"
        self.headers = {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        return _FakeResponse(self._responses.pop(0))


class _FakePostClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        payload, status_code = self._responses.pop(0)
        return _FakeResponse(payload, status_code)


def _ad_row(cost=10.0):
    return {
        "item_id": "MLM1",
        "campaign_id": 101,
        "ad_group_id": 201,
        "status": "active",
        "metrics": {
            "cost": cost,
            "clicks": 2,
            "prints": 100,
            "direct_amount": 20,
            "indirect_amount": 5,
            "total_amount": 25,
            "direct_items_quantity": 1,
            "indirect_items_quantity": 0,
        },
    }


class AdvertisingFetchSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        advertising._items_cache.clear()

    async def test_duplicate_transport_rows_are_counted_once(self):
        row = _ad_row()
        duplicate = copy.deepcopy(row)
        duplicate["status_raw"] = "duplicate transport row"
        payload = {
            "results": [row, duplicate],
            "paging": {"total": 2},
            "metrics_summary": copy.deepcopy(row["metrics"]),
        }
        fake_client = _FakeClient([payload])

        with (
            patch.object(db, "get_token", AsyncMock(return_value={"access_token": "test"})),
            patch.object(advertising.httpx, "AsyncClient", return_value=fake_client),
        ):
            rows = await advertising.fetch_ad_items_for_month(2909534, "2026-07", 3383185411, strict=True)

        self.assertEqual(1, len(rows))
        self.assertEqual(10.0, rows[0]["metrics"]["cost"])

    async def test_summary_mismatch_blocks_strict_fetch(self):
        row = _ad_row(cost=10.0)
        summary = copy.deepcopy(row["metrics"])
        summary["cost"] = 9.0
        payload = {"results": [row], "paging": {"total": 1}, "metrics_summary": summary}
        fake_client = _FakeClient([payload])

        with (
            patch.object(db, "get_token", AsyncMock(return_value={"access_token": "test"})),
            patch.object(advertising.httpx, "AsyncClient", return_value=fake_client),
            self.assertRaisesRegex(RuntimeError, "metrics summary mismatch"),
        ):
            await advertising.fetch_ad_items_for_month(2909534, "2026-07", 3383185411, strict=True)

    async def test_empty_rows_with_nonzero_summary_are_rejected(self):
        summary = copy.deepcopy(_ad_row()["metrics"])
        payload = {"results": [], "paging": {"total": 0}, "metrics_summary": summary}
        fake_client = _FakeClient([payload])

        with (
            patch.object(db, "get_token", AsyncMock(return_value={"access_token": "test"})),
            patch.object(advertising.httpx, "AsyncClient", return_value=fake_client),
            self.assertRaisesRegex(RuntimeError, "metrics summary mismatch"),
        ):
            await advertising.fetch_ad_items_for_month(2909534, "2026-07", 3383185411, strict=True)

    async def test_missing_summary_metric_is_rejected(self):
        row = _ad_row()
        summary = copy.deepcopy(row["metrics"])
        summary.pop("cost")
        payload = {"results": [row], "paging": {"total": 1}, "metrics_summary": summary}
        fake_client = _FakeClient([payload])

        with (
            patch.object(db, "get_token", AsyncMock(return_value={"access_token": "test"})),
            patch.object(advertising.httpx, "AsyncClient", return_value=fake_client),
            self.assertRaisesRegex(RuntimeError, "metrics summary incomplete"),
        ):
            await advertising.fetch_ad_items_for_month(2909534, "2026-07", 3383185411, strict=True)

    async def test_missing_item_id_is_rejected(self):
        row = _ad_row()
        row.pop("item_id")
        payload = {
            "results": [row],
            "paging": {"total": 1},
            "metrics_summary": copy.deepcopy(row["metrics"]),
        }
        fake_client = _FakeClient([payload])

        with (
            patch.object(db, "get_token", AsyncMock(return_value={"access_token": "test"})),
            patch.object(advertising.httpx, "AsyncClient", return_value=fake_client),
            self.assertRaisesRegex(RuntimeError, "item_id missing"),
        ):
            await advertising.fetch_ad_items_for_month(2909534, "2026-07", 3383185411, strict=True)

    async def test_unvalidated_response_is_not_reused_by_strict_sync(self):
        first_payload = {"results": [_ad_row(cost=10.0)], "paging": {"total": 1}}
        second_row = _ad_row(cost=12.0)
        second_payload = {
            "results": [second_row],
            "paging": {"total": 1},
            "metrics_summary": copy.deepcopy(second_row["metrics"]),
        }

        with (
            patch.object(db, "get_token", AsyncMock(return_value={"access_token": "test"})),
            patch.object(advertising.httpx, "AsyncClient", return_value=_FakeClient([first_payload])),
        ):
            first = await advertising.fetch_ad_items_for_month(
                2909534, "2026-07", 3383185411, strict=False
            )
        self.assertEqual(10.0, first[0]["metrics"]["cost"])

        with (
            patch.object(db, "get_token", AsyncMock(return_value={"access_token": "test"})),
            patch.object(advertising.httpx, "AsyncClient", return_value=_FakeClient([second_payload])),
        ):
            strict_rows = await advertising.fetch_ad_items_for_month(
                2909534, "2026-07", 3383185411, strict=True
            )
        self.assertEqual(12.0, strict_rows[0]["metrics"]["cost"])


class MonthlySyncSafetyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _cached_order():
        return {
            "currency": "MXN",
            "_payload": {
                "id": 1,
                "status": "paid",
                "paid_amount": 100,
                "total_amount": 100,
                "currency_id": "MXN",
                "seller": {"site_id": "MLM"},
                "order_items": [
                    {
                        "item": {"id": "MLM1", "seller_sku": "SKU1", "title": "Example"},
                        "currency_id": "MXN",
                        "unit_price": 100,
                        "quantity": 1,
                        "sale_fee": 16,
                    }
                ],
                "payments": [],
            },
        }

    async def test_ad_api_failure_stops_before_feishu_write(self):
        feishu_token = AsyncMock(side_effect=AssertionError("Feishu write must not be reached"))

        with (
            patch.object(db, "cache_list_orders_for_month", AsyncMock(return_value=[self._cached_order()])),
            patch.object(lingxing, "fetch_all_products", AsyncMock(return_value={})),
            patch.object(lingxing, "fetch_fx_rate", AsyncMock(return_value={"MXN": 0.4})),
            patch.object(
                advertising,
                "fetch_ad_items_for_month",
                AsyncMock(side_effect=RuntimeError("ML ads API failed status=429")),
            ),
            patch.object(advertising, "fetch_shop_visits_for_month", AsyncMock(return_value=0)),
            patch.object(main, "_feishu_tenant_token", feishu_token),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=False)

        self.assertEqual(502, ctx.exception.status_code)
        self.assertIn("广告费抓取失败", str(ctx.exception.detail))
        self.assertIn("本次未写入", str(ctx.exception.detail))
        feishu_token.assert_not_awaited()

    async def test_ad_api_failure_commit_records_visible_failure_without_report_write(self):
        feishu_token = AsyncMock(side_effect=AssertionError("Report-table write must not be reached"))
        failure_recorder = AsyncMock(return_value={"status": "ok"})

        with (
            patch.object(db, "cache_list_orders_for_month", AsyncMock(return_value=[self._cached_order()])),
            patch.object(lingxing, "fetch_all_products", AsyncMock(return_value={})),
            patch.object(lingxing, "fetch_fx_rate", AsyncMock(return_value={"MXN": 0.4})),
            patch.object(
                advertising,
                "fetch_ad_items_for_month",
                AsyncMock(side_effect=RuntimeError("ML ads API failed status=429")),
            ),
            patch.object(advertising, "fetch_shop_visits_for_month", AsyncMock(return_value=0)),
            patch.object(ml_close, "record_advertising_failure", failure_recorder, create=True),
            patch.object(main, "_feishu_tenant_token", feishu_token),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=True)

        self.assertIn("广告费抓取失败", str(ctx.exception.detail))
        failure_recorder.assert_awaited_once()
        self.assertEqual("month_2026-07", failure_recorder.await_args.kwargs["period"])
        self.assertEqual("ML 本土3店 DISTRIBUIDOR VALMIGOZ", failure_recorder.await_args.kwargs["shop"])
        self.assertIn("广告费抓取失败", failure_recorder.await_args.kwargs["message"])
        feishu_token.assert_not_awaited()

    async def test_preview_builds_rows_without_feishu_write(self):
        feishu_token = AsyncMock(side_effect=AssertionError("Preview must not request a Feishu token"))
        metrics = {
            "cost": 10.0,
            "clicks": 2.0,
            "prints": 100.0,
            "direct_amount": 20.0,
            "indirect_amount": 5.0,
            "total_amount": 25.0,
            "direct_items_quantity": 1.0,
            "indirect_items_quantity": 0.0,
        }

        with (
            patch.object(db, "cache_list_orders_for_month", AsyncMock(return_value=[self._cached_order()])),
            patch.object(lingxing, "fetch_all_products", AsyncMock(return_value={})),
            patch.object(lingxing, "fetch_fx_rate", AsyncMock(return_value={"MXN": 0.4})),
            patch.object(advertising, "fetch_ad_items_for_month", AsyncMock(return_value=[_ad_row()])),
            patch.object(
                advertising,
                "attribute_ad_metrics_by_item_id",
                AsyncMock(return_value=({"SKU1": metrics}, {}, [])),
            ),
            patch.object(advertising, "fetch_shop_visits_for_month", AsyncMock(return_value=0)),
            patch.object(main, "_feishu_tenant_token", feishu_token),
        ):
            result = await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=False)

        self.assertEqual("preview", result["status"])
        self.assertEqual(10.0, result["ad_total_local"])
        self.assertEqual(4.0, result["ad_total_rmb"])
        self.assertEqual(1, result["rows_previewed"])
        feishu_token.assert_not_awaited()

    async def _run_commit_with_feishu_responses(self, responses):
        metrics = copy.deepcopy(_ad_row()["metrics"])
        fake_client = _FakePostClient(responses)
        clear_recorder = AsyncMock(return_value={"status": "unchanged"})
        with (
            patch.object(db, "cache_list_orders_for_month", AsyncMock(return_value=[self._cached_order()])),
            patch.object(lingxing, "fetch_all_products", AsyncMock(return_value={})),
            patch.object(lingxing, "fetch_fx_rate", AsyncMock(return_value={"MXN": 0.4})),
            patch.object(advertising, "fetch_ad_items_for_month", AsyncMock(return_value=[_ad_row()])),
            patch.object(
                advertising,
                "attribute_ad_metrics_by_item_id",
                AsyncMock(return_value=({"SKU1": metrics}, {}, [])),
            ),
            patch.object(advertising, "fetch_shop_visits_for_month", AsyncMock(return_value=0)),
            patch.object(ml_close, "clear_advertising_failure", clear_recorder),
            patch.object(main, "_feishu_tenant_token", AsyncMock(return_value="test")),
            patch.object(main.httpx, "AsyncClient", return_value=fake_client),
        ):
            result = await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=True)
        self.clear_recorder = clear_recorder
        return result, fake_client

    async def test_feishu_lookup_failure_stops_before_delete_or_create(self):
        fake_client = _FakePostClient([({"code": 999}, 500)])
        metrics = copy.deepcopy(_ad_row()["metrics"])
        with (
            patch.object(db, "cache_list_orders_for_month", AsyncMock(return_value=[self._cached_order()])),
            patch.object(lingxing, "fetch_all_products", AsyncMock(return_value={})),
            patch.object(lingxing, "fetch_fx_rate", AsyncMock(return_value={"MXN": 0.4})),
            patch.object(advertising, "fetch_ad_items_for_month", AsyncMock(return_value=[_ad_row()])),
            patch.object(
                advertising,
                "attribute_ad_metrics_by_item_id",
                AsyncMock(return_value=({"SKU1": metrics}, {}, [])),
            ),
            patch.object(advertising, "fetch_shop_visits_for_month", AsyncMock(return_value=0)),
            patch.object(main, "_feishu_tenant_token", AsyncMock(return_value="test")),
            patch.object(main.httpx, "AsyncClient", return_value=fake_client),
            self.assertRaisesRegex(HTTPException, "pre-write lookup failed"),
        ):
            await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=True)

        self.assertEqual(1, len(fake_client.calls))

    async def test_feishu_delete_failure_rolls_back_new_rows(self):
        fake_client = _FakePostClient([
            ({"code": 0, "data": {"items": [{"record_id": "rec-old"}], "has_more": False}}, 200),
            ({"code": 0, "data": {"records": [{"record_id": "rec-new"}]}}, 200),
            ({"code": 0, "data": {
                "items": [
                    {"record_id": "rec-old", "fields": {}},
                    {"record_id": "rec-new", "fields": {
                        "广告费(原币)": 10.0,
                        "广告费(RMB)": 4.0,
                    }},
                ],
                "has_more": False,
            }}, 200),
            ({"code": 999}, 500),
            ({"code": 0}, 200),
        ])
        metrics = copy.deepcopy(_ad_row()["metrics"])
        with (
            patch.object(db, "cache_list_orders_for_month", AsyncMock(return_value=[self._cached_order()])),
            patch.object(lingxing, "fetch_all_products", AsyncMock(return_value={})),
            patch.object(lingxing, "fetch_fx_rate", AsyncMock(return_value={"MXN": 0.4})),
            patch.object(advertising, "fetch_ad_items_for_month", AsyncMock(return_value=[_ad_row()])),
            patch.object(
                advertising,
                "attribute_ad_metrics_by_item_id",
                AsyncMock(return_value=({"SKU1": metrics}, {}, [])),
            ),
            patch.object(advertising, "fetch_shop_visits_for_month", AsyncMock(return_value=0)),
            patch.object(main, "_feishu_tenant_token", AsyncMock(return_value="test")),
            patch.object(main.httpx, "AsyncClient", return_value=fake_client),
            self.assertRaisesRegex(HTTPException, "replace delete failed"),
        ):
            await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=True)

        self.assertEqual(5, len(fake_client.calls))
        rollback_payload = fake_client.calls[-1][1]["json"]
        self.assertEqual(["rec-new"], rollback_payload["records"])

    async def test_feishu_create_failure_keeps_old_rows(self):
        fake_client = _FakePostClient([
            ({"code": 0, "data": {"items": [{"record_id": "rec-old"}], "has_more": False}}, 200),
            ({"code": 999}, 500),
        ])
        metrics = copy.deepcopy(_ad_row()["metrics"])
        with (
            patch.object(db, "cache_list_orders_for_month", AsyncMock(return_value=[self._cached_order()])),
            patch.object(lingxing, "fetch_all_products", AsyncMock(return_value={})),
            patch.object(lingxing, "fetch_fx_rate", AsyncMock(return_value={"MXN": 0.4})),
            patch.object(advertising, "fetch_ad_items_for_month", AsyncMock(return_value=[_ad_row()])),
            patch.object(
                advertising,
                "attribute_ad_metrics_by_item_id",
                AsyncMock(return_value=({"SKU1": metrics}, {}, [])),
            ),
            patch.object(advertising, "fetch_shop_visits_for_month", AsyncMock(return_value=0)),
            patch.object(main, "_feishu_tenant_token", AsyncMock(return_value="test")),
            patch.object(main.httpx, "AsyncClient", return_value=fake_client),
            self.assertRaisesRegex(HTTPException, "create failed"),
        ):
            await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=True)

        self.assertEqual(2, len(fake_client.calls))

    async def test_feishu_lookup_follows_all_pages_before_replace(self):
        result, fake_client = await self._run_commit_with_feishu_responses([
            ({"code": 0, "data": {
                "items": [{"record_id": "rec-old-1"}],
                "has_more": True,
                "page_token": "next-page",
            }}, 200),
            ({"code": 0, "data": {
                "items": [{"record_id": "rec-old-2"}],
                "has_more": False,
            }}, 200),
            ({"code": 0, "data": {"records": [{"record_id": "rec-new"}]}}, 200),
            ({"code": 0, "data": {
                "items": [
                    {"record_id": "rec-old-1", "fields": {}},
                    {"record_id": "rec-old-2", "fields": {}},
                    {"record_id": "rec-new", "fields": {
                        "广告费(原币)": 10.0,
                        "广告费(RMB)": 4.0,
                    }},
                ],
                "has_more": False,
            }}, 200),
            ({"code": 0}, 200),
            ({"code": 0, "data": {
                "items": [{"record_id": "rec-new", "fields": {
                    "广告费(原币)": 10.0,
                    "广告费(RMB)": 4.0,
                }}],
                "has_more": False,
            }}, 200),
        ])

        self.assertEqual(2, result["rows_replaced"])
        self.assertEqual(1, result["rows_verified"])
        self.assertEqual(6, len(fake_client.calls))
        self.clear_recorder.assert_awaited_once()
        self.assertEqual(
            ("month_2026-07", "ML 本土3店 DISTRIBUIDOR VALMIGOZ"),
            self.clear_recorder.await_args.args,
        )
        self.assertGreater(self.clear_recorder.await_args.kwargs["success_started_at"], 0)


class MonthlyCloseAdvertisingFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_time_is_captured_before_waiting_for_month_lock(self):
        period = "month_2026-07"
        shared = {"record_id": "rec-status", "fields": {}}

        async def read_status(*args, **kwargs):
            return copy.deepcopy(shared)

        async def write_status(write_period, fields, tok=None):
            shared["fields"].update(copy.deepcopy(fields))
            return {"record_id": "rec-status"}

        lock = ml_close._status_mutation_lock(period)
        await lock.acquire()
        try:
            with (
                patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
                patch.object(ml_close, "_get_status", side_effect=read_status),
                patch.object(ml_close, "_upsert_status", side_effect=write_status),
            ):
                failure_task = asyncio.create_task(
                    ml_close.record_advertising_failure(
                        period,
                        "ML 本土3店",
                        "广告费抓取失败",
                        send=False,
                    )
                )
                await asyncio.sleep(0.03)
                later_success_started_at = time.time_ns()
                await asyncio.sleep(0.03)
                lock.release()
                await failure_task
                result = await ml_close.clear_advertising_failure(
                    period,
                    "ML 本土3店",
                    success_started_at=later_success_started_at,
                )
        finally:
            if lock.locked():
                lock.release()

        self.assertEqual("cleared", result["status"])

    async def test_none_card_request_is_forced_to_error_by_durable_failure(self):
        self.db_list_failures.return_value = [{"shop": "ML 本土3店"}]
        confirmed = {"record_id": "rec-status", "fields": {"状态": "运营已确认"}}

        with patch.object(ml_close, "_get_status", AsyncMock(return_value=confirmed)):
            result = await ml_close.card_endpoint(
                kind="none",
                period="month_2026-07",
                send=False,
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual("error", result["kind"])
        self.assertIn("广告费抓取失败", json.dumps(result["card"], ensure_ascii=False))

    async def test_deduped_confirmation_is_still_blocked_by_durable_failure(self):
        self.db_list_failures.return_value = [{"shop": "ML 本土3店"}]
        current = {
            "record_id": "rec-status",
            "fields": {
                "状态": "运营已确认",
                "最后按钮动作Key": "om-old:ml_profit_ops_confirm",
            },
        }
        feedback = AsyncMock(return_value={"patched": True})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=current)),
            patch.object(ml_close, "patch_or_fallback", feedback),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_ops_confirm",
                "period": "month_2026-07",
                "message_id": "om-old",
                "operator_name": "运营",
            })

        self.assertEqual("blocked", result["status"])
        self.assertNotIn("deduped", result)
        self.assertIn("广告费抓取失败", result["reason"])
        feedback.assert_awaited_once()

    async def test_failure_arriving_during_audit_cannot_be_overwritten(self):
        prior = {"record_id": "rec-status", "fields": {"状态": "待运营确认"}}
        self.db_list_failures.side_effect = [[], [{"shop": "ML 本土3店"}]]
        status_writer = AsyncMock(return_value={"record_id": "rec-status"})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=prior)),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=[])),
            patch.object(ml_close, "_upsert_status", status_writer),
        ):
            result = await ml_close.audit(
                period="month_2026-07",
                commit=True,
                run_cost_preview=False,
                cost_summary={"status": "ok"},
            )

        self.assertEqual("异常", result["state"])
        self.assertEqual("error", result["next_card"])
        self.assertEqual("异常", status_writer.await_args.args[1]["状态"])
        self.assertIn("广告费抓取失败", status_writer.await_args.args[1]["最后错误"])

    async def test_explicit_confirmation_card_kind_cannot_override_ad_failure(self):
        status = {"record_id": "rec-status", "fields": {"状态": "异常"}}
        blocked_summary = {
            "status": "error",
            "period": "month_2026-07",
            "month": "2026-07",
            "state": "异常",
            "next_card": "error",
            "last_error": "广告费抓取失败：ML 本土3店。",
        }

        with (
            patch.object(ml_close, "_get_status", AsyncMock(return_value=status)),
            patch.object(ml_close, "audit", AsyncMock(return_value=blocked_summary)),
        ):
            result = await ml_close.card_endpoint(
                kind="ops_final",
                period="month_2026-07",
                send=False,
            )

        self.assertEqual("error", result["kind"])
        self.assertIn("广告费抓取失败", json.dumps(result["card"], ensure_ascii=False))

    def setUp(self):
        ml_close._EMERGENCY_AD_FAILURES.clear()
        self.db_set_patcher = patch.object(db, "set_ad_sync_failure", AsyncMock())
        self.db_clear_patcher = patch.object(db, "clear_ad_sync_failure", AsyncMock())
        self.db_list_patcher = patch.object(db, "list_ad_sync_failures", AsyncMock(return_value=[]))
        self.fallback_set_patcher = patch.object(db, "set_ad_sync_failure_fallback", AsyncMock())
        self.fallback_clear_patcher = patch.object(db, "clear_ad_sync_failure_fallback", AsyncMock())
        self.fallback_list_patcher = patch.object(db, "list_ad_sync_failure_fallbacks", AsyncMock(return_value=[]))
        self.db_set_failure = self.db_set_patcher.start()
        self.db_clear_failure = self.db_clear_patcher.start()
        self.db_list_failures = self.db_list_patcher.start()
        self.fallback_set_failure = self.fallback_set_patcher.start()
        self.fallback_clear_failure = self.fallback_clear_patcher.start()
        self.fallback_list_failures = self.fallback_list_patcher.start()

    def tearDown(self):
        ml_close._EMERGENCY_AD_FAILURES.clear()
        self.db_set_patcher.stop()
        self.db_clear_patcher.stop()
        self.db_list_patcher.stop()
        self.fallback_set_patcher.stop()
        self.fallback_clear_patcher.stop()
        self.fallback_list_patcher.stop()

    async def test_status_gate_uses_durable_failure_even_if_feishu_says_confirmed(self):
        self.db_list_failures.return_value = [{"shop": "ML 本土3店"}]
        confirmed = {"record_id": "rec-status", "fields": {"状态": "运营已确认"}}

        with patch.object(ml_close, "_get_status", AsyncMock(return_value=confirmed)):
            result = await ml_close.status_endpoint(period="month_2026-07")

        self.assertEqual("异常", result["state"])
        self.assertFalse(result["ready_for_finance"])
        self.assertEqual(["ML 本土3店"], result["failed_ad_shops"])

    async def test_failure_card_still_sends_when_status_ledger_write_fails(self):
        card_sender = AsyncMock(return_value={"data": {"message_id": "om-fallback"}})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(side_effect=RuntimeError("status token unavailable"))),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=None)),
            patch.object(ml_close, "_upsert_status", AsyncMock()),
            patch.object(ml_close, "send_card", card_sender),
        ):
            result = await ml_close.record_advertising_failure(
                "month_2026-07",
                "ML 本土3店",
                "广告费抓取失败",
            )

        self.assertEqual("om-fallback", result["message_id"])
        self.assertIn("status token unavailable", result["status_write_error"])
        card_sender.assert_awaited_once()

    async def test_emergency_gate_blocks_when_both_persistent_writes_fail(self):
        self.db_set_failure.side_effect = RuntimeError("sqlite unavailable")
        self.fallback_set_failure.side_effect = RuntimeError("fallback unavailable")
        confirmed = {"record_id": "rec-status", "fields": {"状态": "运营已确认"}}
        card_sender = AsyncMock(return_value={"data": {"message_id": "om-fallback"}})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(side_effect=RuntimeError("status unavailable"))),
            patch.object(ml_close, "send_card", card_sender),
        ):
            with self.assertRaisesRegex(RuntimeError, "no persistent confirmation gate"):
                await ml_close.record_advertising_failure(
                    "month_2026-07",
                    "ML 本土3店",
                    "广告费抓取失败",
                )

        with patch.object(ml_close, "_get_status", AsyncMock(return_value=confirmed)):
            status = await ml_close.status_endpoint(period="month_2026-07")

        card_sender.assert_awaited_once()
        self.assertEqual("异常", status["state"])
        self.assertFalse(status["ready_for_finance"])
        self.assertEqual(["ML 本土3店"], status["failed_ad_shops"])

    async def test_concurrent_shop_failures_do_not_overwrite_each_other(self):
        shared = {"record_id": "rec-status", "fields": {"最后结果JSON": "{}"}}

        async def read_status(*args, **kwargs):
            snapshot = copy.deepcopy(shared)
            await asyncio.sleep(0.01)
            return snapshot

        async def write_status(period, fields, tok=None):
            await asyncio.sleep(0.01)
            shared["fields"].update(copy.deepcopy(fields))
            return {"record_id": "rec-status"}

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", side_effect=read_status),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
        ):
            await asyncio.gather(
                ml_close.record_advertising_failure(
                    "month_2026-07", "ML 本土2店", "广告费抓取失败", send=False
                ),
                ml_close.record_advertising_failure(
                    "month_2026-07", "ML 本土3店", "广告费抓取失败", send=False
                ),
            )

        stored = json.loads(shared["fields"]["最后结果JSON"])
        self.assertEqual(["ML 本土2店", "ML 本土3店"], stored["failed_ad_shops"])

    async def test_stale_confirmation_card_cannot_bypass_open_ad_failure(self):
        current = {
            "record_id": "rec-status",
            "fields": {
                "状态": "异常",
                "最后结果JSON": json.dumps(
                    {"failure_type": "advertising_fetch", "failed_ad_shops": ["ML 本土3店"]},
                    ensure_ascii=False,
                ),
            },
        }
        status_writer = AsyncMock(return_value={"record_id": "rec-status"})
        card_sender = AsyncMock(return_value={"data": {"message_id": "om-finance"}})
        feedback = AsyncMock(return_value={"patched": True})
        blocked_summary = {
            "status": "error",
            "period": "month_2026-07",
            "month": "2026-07",
            "state": "异常",
            "next_card": "error",
            "last_error": "广告费抓取失败：ML 本土3店。",
            "failed_ad_shops": ["ML 本土3店"],
        }

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=current)),
            patch.object(ml_close, "_upsert_status", status_writer),
            patch.object(ml_close, "audit", AsyncMock(return_value=blocked_summary)),
            patch.object(ml_close, "patch_or_fallback", feedback),
            patch.object(ml_close, "send_card", card_sender),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_ops_confirm",
                "period": "month_2026-07",
                "message_id": "om-old-green-card",
                "operator_name": "运营",
            })

        self.assertEqual("blocked", result["status"])
        self.assertIn("广告费抓取失败", result["reason"])
        for call in status_writer.await_args_list:
            self.assertNotEqual("运营已确认", call.args[1].get("状态"))
        card_sender.assert_not_awaited()
        feedback.assert_awaited_once()

    async def test_failure_arriving_after_initial_callback_check_blocks_finance_card(self):
        self.db_list_failures.side_effect = [[], [{"shop": "ML 本土3店"}]]
        current = {"record_id": "rec-status", "fields": {"状态": "待运营确认"}}
        clean_summary = {
            "status": "ok",
            "period": "month_2026-07",
            "month": "2026-07",
            "state": "待运营确认",
            "next_card": "ops_final",
            "last_error": "",
        }
        status_writer = AsyncMock(return_value={"record_id": "rec-status"})
        card_sender = AsyncMock(return_value={"data": {"message_id": "om-finance"}})
        feedback = AsyncMock(return_value={"patched": True})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=current)),
            patch.object(ml_close, "_upsert_status", status_writer),
            patch.object(ml_close, "audit", AsyncMock(return_value=clean_summary)),
            patch.object(ml_close, "patch_or_fallback", feedback),
            patch.object(ml_close, "send_card", card_sender),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_ops_confirm",
                "period": "month_2026-07",
                "message_id": "om-old-green-card",
                "operator_name": "运营",
            })

        self.assertEqual("blocked", result["status"])
        self.assertIn("广告费抓取失败", result["reason"])
        for call in status_writer.await_args_list:
            self.assertNotEqual("运营已确认", call.args[1].get("状态"))
        card_sender.assert_not_awaited()
        feedback.assert_awaited_once()

    async def test_preview_audit_also_blocks_confirmation_while_ad_failure_is_open(self):
        prior = {
            "record_id": "rec-status",
            "fields": {
                "状态": "异常",
                "最后结果JSON": json.dumps(
                    {"failure_type": "advertising_fetch", "failed_ad_shops": ["ML 本土3店"]},
                    ensure_ascii=False,
                ),
            },
        }

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=prior)),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=[])),
        ):
            result = await ml_close.audit(
                period="month_2026-07",
                commit=False,
                run_cost_preview=False,
                cost_summary={"status": "ok"},
            )

        self.assertEqual("异常", result["state"])
        self.assertEqual("error", result["next_card"])
        self.assertIn("广告费抓取失败", result["last_error"])

    async def test_last_successful_shop_moves_failure_to_recalc_state(self):
        prior = {
            "record_id": "rec-status",
            "fields": {
                "状态": "异常",
                "最后结果JSON": json.dumps(
                    {"failure_type": "advertising_fetch", "failed_ad_shops": ["ML 本土3店"]},
                    ensure_ascii=False,
                ),
            },
        }
        status_writer = AsyncMock(return_value={"record_id": "rec-status"})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=prior)),
            patch.object(ml_close, "_upsert_status", status_writer),
        ):
            result = await ml_close.clear_advertising_failure("month_2026-07", "ML 本土3店")

        fields = status_writer.await_args.args[1]
        self.assertEqual("退回重算", fields["状态"])
        self.assertEqual("", fields["最后错误"])
        self.assertEqual([], json.loads(fields["最后结果JSON"])["failed_ad_shops"])
        self.assertEqual("cleared", result["status"])
        self.db_clear_failure.assert_awaited_once_with("month_2026-07", "ML 本土3店")

    async def test_status_write_failure_keeps_persistent_marker_before_clear(self):
        self.fallback_list_failures.return_value = [{
            "shop": "ML 本土3店",
            "failed_at": 10,
        }]
        confirmed = {"record_id": "rec-status", "fields": {"状态": "运营已确认"}}
        status_writer = AsyncMock(side_effect=RuntimeError("status write unavailable"))

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=confirmed)),
            patch.object(ml_close, "_upsert_status", status_writer),
        ):
            with self.assertRaisesRegex(RuntimeError, "status write unavailable"):
                await ml_close.clear_advertising_failure(
                    "month_2026-07",
                    "ML 本土3店",
                    success_started_at=20,
                )

        self.fallback_clear_failure.assert_not_awaited()
        self.db_clear_failure.assert_not_awaited()

    async def test_successful_shop_sync_clears_only_its_ad_failure(self):
        prior = {
            "record_id": "rec-status",
            "fields": {
                "状态": "异常",
                "最后结果JSON": json.dumps(
                    {
                        "failure_type": "advertising_fetch",
                        "failed_ad_shops": ["ML 本土2店", "ML 本土3店"],
                    },
                    ensure_ascii=False,
                ),
            },
        }
        status_writer = AsyncMock(return_value={"record_id": "rec-status"})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=prior)),
            patch.object(ml_close, "_upsert_status", status_writer),
        ):
            result = await ml_close.clear_advertising_failure("month_2026-07", "ML 本土3店")

        fields = status_writer.await_args.args[1]
        stored = json.loads(fields["最后结果JSON"])
        self.assertEqual(["ML 本土2店"], stored["failed_ad_shops"])
        self.assertEqual("异常", fields["状态"])
        self.assertIn("ML 本土2店", fields["最后错误"])
        self.assertEqual(["ML 本土2店"], result["failed_ad_shops"])

    async def test_record_ad_failure_keeps_all_failed_shops_and_sends_visible_card(self):
        prior = {
            "record_id": "rec-status",
            "fields": {
                "最后结果JSON": json.dumps(
                    {"failure_type": "advertising_fetch", "failed_ad_shops": ["ML 本土2店"]},
                    ensure_ascii=False,
                ),
            },
        }
        status_writer = AsyncMock(return_value={"record_id": "rec-status"})
        card_sender = AsyncMock(return_value={"data": {"message_id": "om-test"}})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=prior)),
            patch.object(ml_close, "_upsert_status", status_writer),
            patch.object(ml_close, "send_card", card_sender),
        ):
            result = await ml_close.record_advertising_failure(
                period="month_2026-07",
                shop="ML 本土3店",
                message="广告费抓取失败：ML 本土3店 / 2026-07。本次未写入。",
            )

        first_fields = status_writer.await_args_list[0].args[1]
        stored = json.loads(first_fields["最后结果JSON"])
        self.assertEqual(["ML 本土2店", "ML 本土3店"], stored["failed_ad_shops"])
        self.assertEqual("advertising_fetch", stored["failure_type"])
        self.assertIn("广告费抓取失败", json.dumps(result["card"], ensure_ascii=False))
        self.assertEqual("om-test", result["message_id"])
        self.db_set_failure.assert_awaited_once()

    async def test_audit_blocks_ops_confirmation_while_ad_failure_is_open(self):
        prior = {
            "record_id": "rec-status",
            "fields": {
                "状态": "异常",
                "最后错误": "广告费抓取失败：ML 本土3店 / 2026-07",
                "最后结果JSON": json.dumps(
                    {"failure_type": "advertising_fetch", "failed_ad_shops": ["ML 本土3店"]},
                    ensure_ascii=False,
                ),
            },
        }
        report_rows = [{
            "record_id": "rec-report",
            "fields": {
                "周期": "month_2026-07",
                "店铺": "ML 本土3店",
                "SKU": "SKU1",
                "营收(RMB)": 100.0,
                "广告费(RMB)": 0.0,
                "采购成本(RMB)": 20.0,
                "头程成本(RMB)": 5.0,
                "全额毛利(RMB)": 75.0,
                "订单数": 1,
                "件数": 1,
            },
        }]
        status_writer = AsyncMock(return_value={"record_id": "rec-status"})

        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="test")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=prior)),
            patch.object(ml_close, "_list_records", AsyncMock(return_value=report_rows)),
            patch.object(ml_close, "_upsert_status", status_writer),
        ):
            result = await ml_close.audit(
                period="month_2026-07",
                commit=True,
                run_cost_preview=False,
                cost_summary={"status": "ok", "detail": "x" * 5000},
            )

        self.assertEqual("异常", result["state"])
        self.assertEqual("error", result["next_card"])
        self.assertIn("广告费抓取失败", result["last_error"])
        self.assertEqual(["ML 本土3店"], result["failed_ad_shops"])
        stored = json.loads(status_writer.await_args.args[1]["最后结果JSON"])
        self.assertEqual(["ML 本土3店"], stored["failed_ad_shops"])


class DurableAdFailureStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_marker_survives_restart_when_sqlite_and_status_write_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml-sync-test.db")
            confirmed = {"record_id": "rec-status", "fields": {"状态": "运营已确认"}}
            card_sender = AsyncMock(return_value={"data": {"message_id": "om-fallback"}})
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                with (
                    patch.object(db, "set_ad_sync_failure", AsyncMock(side_effect=RuntimeError("sqlite unavailable"))),
                    patch.object(ml_close, "_tenant_token", AsyncMock(side_effect=RuntimeError("status unavailable"))),
                    patch.object(ml_close, "send_card", card_sender),
                ):
                    await ml_close.record_advertising_failure(
                        "month_2026-07",
                        "ML 本土3店",
                        "广告费抓取失败",
                    )

                ml_close._EMERGENCY_AD_FAILURES.clear()
                with patch.object(ml_close, "_get_status", AsyncMock(return_value=confirmed)):
                    status = await ml_close.status_endpoint(period="month_2026-07")

            self.assertEqual("异常", status["state"])
            self.assertFalse(status["ready_for_finance"])
            self.assertEqual(["ML 本土3店"], status["failed_ad_shops"])

    async def test_old_success_cannot_clear_a_newer_failure_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml-sync-test.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.set_ad_sync_failure("month_2026-07", "ML 本土3店", "广告费抓取失败")
                cleared = await db.clear_ad_sync_failure(
                    "month_2026-07",
                    "ML 本土3店",
                    not_after=0,
                )
                self.assertFalse(cleared)
                self.assertEqual(
                    ["ML 本土3店"],
                    [row["shop"] for row in await db.list_ad_sync_failures("month_2026-07")],
                )
                cleared = await db.clear_ad_sync_failure(
                    "month_2026-07",
                    "ML 本土3店",
                    not_after=2**63 - 1,
                )
                self.assertTrue(cleared)

    async def test_failure_marker_survives_independent_database_connections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml-sync-test.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.set_ad_sync_failure("month_2026-07", "ML 本土3店", "广告费抓取失败")
                rows = await db.list_ad_sync_failures("month_2026-07")
                self.assertEqual(["ML 本土3店"], [row["shop"] for row in rows])
                await db.clear_ad_sync_failure("month_2026-07", "ML 本土3店")
                self.assertEqual([], await db.list_ad_sync_failures("month_2026-07"))


if __name__ == "__main__":
    unittest.main()
