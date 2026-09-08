import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException

from app import main, ml_close


class MonthBackfillTests(unittest.IsolatedAsyncioTestCase):
    def test_sales_date_uses_site_local_date_closed(self):
        order = {"id": 101, "date_closed": "2026-08-31T23:31:40.000-04:00"}

        self.assertEqual("2026-09-01", main._site_local_closed_date(2378517428, order))
        self.assertEqual("2026-08-31", main._site_local_closed_date(3383185411, order))

    async def test_local_month_scope_uses_site_date_closed_and_excludes_cancelled(self):
        search = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "results": [{"id": 101}, {"id": 102}, {"id": 103}, {"id": 104}],
                "paging": {"total": 4},
            },
        )

        def detail(order_id, *, status="paid", date_closed=None):
            payload = {
                "id": order_id,
                "status": status,
                "date_created": "2026-08-31T23:30:00.000-04:00",
                "date_closed": date_closed,
                "currency_id": "MXN",
                "paid_amount": 100,
                "total_amount": 100,
                "payments": [],
                "shipping": {"id": order_id + 1000},
                "order_items": [{
                    "item": {"id": f"MLM{order_id}", "seller_sku": "SKU1"},
                    "quantity": 1,
                    "unit_price": 100,
                    "currency_id": "MXN",
                    "sale_fee": 16,
                }],
            }
            return SimpleNamespace(status_code=200, headers={}, json=lambda: payload)

        responses = [
            search,
            detail(101, date_closed="2026-08-31T23:30:00.000-06:00"),
            detail(102, date_closed="2026-09-01T02:27:16.000-04:00"),
            detail(103, status="cancelled", date_closed="2026-08-15T10:00:00.000-06:00"),
            detail(104, status="payment_in_process", date_closed=None),
        ]
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_mx"})),
            patch.object(main.db, "cache_get_order", AsyncMock(return_value=None)),
            patch.object(main.db, "cache_put_order", AsyncMock()),
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
            patch.object(main, "_ml_get", AsyncMock(side_effect=responses)),
        ):
            result = await main._report_sku_recent_impl(
                3383185411,
                200,
                3383185411,
                date_from="2026-07-01T00:00:00.000-06:00",
                date_to="2026-09-03T00:00:00.000-06:00",
                max_detail_fetch=100,
                refresh_after=1000,
                scope_month="2026-08",
                scope_timezone="America/Mexico_City",
            )

        self.assertEqual(result["platform_total"], 4)
        self.assertEqual(result["_month_scope_order_ids"], [101])
        self.assertEqual(result["month_scope_total"], 1)
        self.assertEqual(result["month_scope_unclosed_excluded"], 1)

    async def test_paid_order_without_date_closed_fails_month_scope(self):
        search = SimpleNamespace(
            status_code=200,
            json=lambda: {"results": [{"id": 101}], "paging": {"total": 1}},
        )
        detail = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {
                "id": 101,
                "status": "paid",
                "date_created": "2026-08-31T23:30:00.000-06:00",
                "date_closed": None,
                "currency_id": "MXN",
                "paid_amount": 100,
                "total_amount": 100,
                "payments": [],
                "shipping": {"id": 1101},
                "order_items": [{
                    "item": {"id": "MLM101", "seller_sku": "SKU1"},
                    "quantity": 1,
                    "unit_price": 100,
                    "currency_id": "MXN",
                    "sale_fee": 16,
                }],
            },
        )
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_mx"})),
            patch.object(main.db, "cache_get_order", AsyncMock(return_value=None)),
            patch.object(main.db, "cache_put_order", AsyncMock()),
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
            patch.object(main, "_ml_get", AsyncMock(side_effect=[search, detail])),
        ):
            with self.assertRaisesRegex(HTTPException, "monthly paid order missing date_closed"):
                await main._report_sku_recent_impl(
                    3383185411,
                    200,
                    3383185411,
                    date_from="2026-07-01T00:00:00.000-06:00",
                    date_to="2026-09-03T00:00:00.000-06:00",
                    max_detail_fetch=100,
                    refresh_after=1000,
                    scope_month="2026-08",
                    scope_timezone="America/Mexico_City",
                )

    async def test_local_search_uses_order_prefixed_date_filter(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"results": [], "paging": {"total": 0}})
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_br"})),
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
            patch.object(main, "_ml_get", AsyncMock(return_value=response)) as request,
        ):
            await main._report_sku_recent_impl(
                2378517428,
                200,
                2378517428,
                date_from="2026-08-01T00:00:00.000-03:00",
                date_to="2026-09-01T00:00:00.000-03:00",
                max_detail_fetch=1,
            )

        params = request.await_args.args[3]
        self.assertIn("order.date_created.from", params)
        self.assertNotIn("date_created.from", params)

    async def test_cbt_search_keeps_unprefixed_date_filter(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"results": [], "paging": {"total": 0}})
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "cbt"})),
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
            patch.object(main, "_ml_get", AsyncMock(return_value=response)) as request,
        ):
            await main._report_sku_recent_impl(
                1502236229,
                200,
                1502520822,
                date_from="2026-08-01T00:00:00.000-00:00",
                date_to="2026-09-01T00:00:00.000-00:00",
                max_detail_fetch=1,
            )

        params = request.await_args.args[3]
        self.assertIn("date_created.from", params)
        self.assertNotIn("order.date_created.from", params)

    async def test_backfill_uses_wide_search_and_site_local_closed_month_scope(self):
        result = {
            "packs_returned": 0,
            "orders_with_detail": 0,
            "new_fetches": 0,
            "skipped_429": 0,
            "skipped_other": 0,
            "capped": False,
            "platform_total": 0,
            "_platform_order_ids": [],
        }
        with (
            patch.object(main, "_report_sku_recent_impl", AsyncMock(return_value=result)) as impl,
            patch.object(main.db, "cache_replace_month_scope", AsyncMock(return_value=0)),
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
        ):
            await main.admin_backfill_orders(2378517428, month="2026-08", refresh_after=1000)
        self.assertEqual(impl.await_args.kwargs["date_from"], "2026-07-01T00:00:00.000-03:00")
        self.assertEqual(impl.await_args.kwargs["date_to"], "2026-09-03T00:00:00.000-03:00")
        self.assertEqual(impl.await_args.kwargs["scope_month"], "2026-08")
        self.assertEqual(impl.await_args.kwargs["scope_timezone"], "America/Sao_Paulo")

        with (
            patch.object(main, "_report_sku_recent_impl", AsyncMock(return_value=result)) as impl,
            patch.object(main.db, "cache_replace_month_scope", AsyncMock(return_value=0)),
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
        ):
            await main.admin_backfill_orders(3383185411, month="2026-08", refresh_after=1000)
        self.assertEqual(impl.await_args.kwargs["date_from"], "2026-07-01T00:00:00.000-06:00")
        self.assertEqual(impl.await_args.kwargs["date_to"], "2026-09-03T00:00:00.000-06:00")
        self.assertEqual(impl.await_args.kwargs["scope_month"], "2026-08")
        self.assertEqual(impl.await_args.kwargs["scope_timezone"], "America/Mexico_City")

    async def test_complete_backfill_replaces_authoritative_platform_month_scope(self):
        result = {
            "packs_returned": 2,
            "platform_total": 2,
            "orders_with_detail": 2,
            "cached_month_unique": 3,
            "new_fetches": 0,
            "skipped_429": 0,
            "skipped_other": 0,
            "capped": False,
            "_platform_order_ids": [101, 102],
            "_month_scope_order_ids": [101],
            "month_scope_total": 1,
        }
        cached = [{"order_id": 101}]
        with (
            patch.object(main, "_report_sku_recent_impl", AsyncMock(return_value=result)),
            patch.object(main.db, "cache_replace_month_scope", AsyncMock(return_value=1)) as replace,
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=cached)),
        ):
            response = await main.admin_backfill_orders(2378517428, month="2026-08", refresh_after=1000)

        replace.assert_awaited_once_with(2378517428, "2026-08", [101])
        self.assertEqual(response["cached_month_unique"], 1)
        self.assertEqual(response["month_scope_total"], 1)
        self.assertTrue(response["month_scope_replaced"])

    async def test_month_backfill_requires_a_stable_refresh_cutoff(self):
        with self.assertRaisesRegex(HTTPException, "refresh_after"):
            await main.admin_backfill_orders(2378517428, month="2026-08")

    async def test_month_backfill_rejects_a_cutoff_ahead_of_service_clock(self):
        with (
            patch.object(main.time, "time", return_value=1000),
            self.assertRaisesRegex(HTTPException, "service clock"),
        ):
            await main.admin_backfill_orders(
                2378517428,
                month="2026-08",
                refresh_after=1031,
            )

    async def test_month_backfill_refreshes_stale_cached_order_details(self):
        search = SimpleNamespace(
            status_code=200,
            json=lambda: {"results": [{"id": 101}], "paging": {"total": 1}},
        )
        detail_payload = {
            "id": 101,
            "status": "cancelled",
            "date_created": "2026-08-10T10:00:00.000-03:00",
            "order_items": [],
        }
        detail = SimpleNamespace(status_code=200, json=lambda: detail_payload)
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_br"})),
            patch.object(main.db, "cache_get_order", AsyncMock(return_value={"fetched_at": 999, "_payload": {"id": 101, "status": "paid"}})),
            patch.object(main.db, "cache_put_order", AsyncMock()) as cache_put,
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
            patch.object(main, "_ml_get", AsyncMock(side_effect=[search, detail])) as request,
        ):
            result = await main._report_sku_recent_impl(
                2378517428,
                200,
                2378517428,
                date_from="2026-08-01T00:00:00.000-03:00",
                date_to="2026-09-01T00:00:00.000-03:00",
                max_detail_fetch=100,
                refresh_after=1000,
            )

        self.assertEqual(request.await_count, 2)
        cache_put.assert_awaited_once_with(101, 2378517428, detail_payload)
        self.assertEqual(result["new_fetches"], 1)
        self.assertEqual(result["orders_with_detail"], 1)

    async def test_month_backfill_accepts_partial_content_order_detail(self):
        search = SimpleNamespace(
            status_code=200,
            json=lambda: {"results": [{"id": 101}], "paging": {"total": 1}},
        )
        detail_payload = {
            "id": 101,
            "status": "paid",
            "date_created": "2026-08-10T10:00:00.000-03:00",
            "currency_id": "BRL",
            "paid_amount": 100,
            "total_amount": 100,
            "payments": [],
            "shipping": {"id": 501},
            "order_items": [{
                "item": {"id": "MLB1", "seller_sku": "SKU1"},
                "quantity": 1,
                "unit_price": 100,
                "currency_id": "BRL",
                "sale_fee": 10,
            }],
        }
        detail = SimpleNamespace(
            status_code=206,
            headers={"x-content-missing": "buyer,feedback"},
            json=lambda: detail_payload,
        )
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_br"})),
            patch.object(main.db, "cache_get_order", AsyncMock(return_value=None)),
            patch.object(main.db, "cache_put_order", AsyncMock()) as cache_put,
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
            patch.object(main, "_ml_get", AsyncMock(side_effect=[search, detail])),
        ):
            result = await main._report_sku_recent_impl(
                2378517428,
                200,
                2378517428,
                date_from="2026-08-01T00:00:00.000-03:00",
                date_to="2026-09-01T00:00:00.000-03:00",
                max_detail_fetch=100,
                refresh_after=1000,
            )

        cache_put.assert_awaited_once_with(101, 2378517428, detail_payload)
        self.assertEqual(result["orders_with_detail"], 1)
        self.assertEqual(result["skipped_other"], 0)
        self.assertEqual(result["partial_details"], [{"order_id": 101, "content_missing": "buyer,feedback"}])

    def test_partial_detail_rejects_null_or_nonfinite_financial_amounts(self):
        base = {
            "id": 101,
            "status": "paid",
            "date_created": "2026-08-10T10:00:00.000-03:00",
            "currency_id": "BRL",
            "paid_amount": 100,
            "total_amount": 100,
            "payments": [],
            "shipping": {"id": 501},
            "order_items": [{
                "item": {"id": "MLB1", "seller_sku": "SKU1"},
                "quantity": 1,
                "unit_price": 100,
                "currency_id": "BRL",
                "sale_fee": 10,
            }],
        }
        response = SimpleNamespace(headers={"x-content-missing": "buyer"})
        cases = [
            ("sale_fee", lambda d: d["order_items"][0].__setitem__("sale_fee", None), "order_items[0].sale_fee"),
            ("unit_price", lambda d: d["order_items"][0].__setitem__("unit_price", float("nan")), "order_items[0].unit_price_currency"),
            ("paid_amount", lambda d: d.__setitem__("paid_amount", float("inf")), "paid_amount"),
            ("total_amount", lambda d: d.__setitem__("total_amount", -1), "total_amount"),
        ]
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                detail = copy.deepcopy(base)
                mutate(detail)
                self.assertIn(expected, main._monthly_detail_financial_issues(detail, response))

    async def test_month_backfill_rejects_partial_detail_missing_financial_fields(self):
        search = SimpleNamespace(
            status_code=200,
            json=lambda: {"results": [{"id": 101}], "paging": {"total": 1}},
        )
        detail_payload = {
            "id": 101,
            "status": "paid",
            "date_created": "2026-08-10T10:00:00.000-03:00",
            "currency_id": "BRL",
            "paid_amount": 100,
            "total_amount": 100,
            "payments": [],
            "order_items": [{
                "item": {"id": "MLB1", "seller_sku": "SKU1"},
                "quantity": 1,
                "unit_price": 100,
                "currency_id": "BRL",
                "sale_fee": 10,
            }],
        }
        detail = SimpleNamespace(
            status_code=206,
            headers={"x-content-missing": "shipping"},
            json=lambda: detail_payload,
        )
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_br"})),
            patch.object(main.db, "cache_get_order", AsyncMock(return_value=None)),
            patch.object(main.db, "cache_put_order", AsyncMock()) as cache_put,
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
            patch.object(main, "_ml_get", AsyncMock(side_effect=[search, detail])),
        ):
            result = await main._report_sku_recent_impl(
                2378517428,
                200,
                2378517428,
                date_from="2026-08-01T00:00:00.000-03:00",
                date_to="2026-09-01T00:00:00.000-03:00",
                max_detail_fetch=100,
                refresh_after=1000,
            )

        cache_put.assert_not_awaited()
        self.assertEqual(result["orders_with_detail"], 0)
        self.assertEqual(result["skipped_other"], 1)
        self.assertIn("header:shipping", result["detail_failures"][0]["issues"])

    async def test_monthly_search_rejects_missing_platform_total(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"results": []})
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_br"})),
            patch.object(main, "_ml_get", AsyncMock(return_value=response)),
        ):
            with self.assertRaisesRegex(HTTPException, "paging.total"):
                await main._report_sku_recent_impl(
                    2378517428,
                    200,
                    2378517428,
                    date_from="2026-08-01T00:00:00.000-03:00",
                    date_to="2026-09-01T00:00:00.000-03:00",
                    max_detail_fetch=1,
                )

    async def test_committed_sync_rejects_empty_cache(self):
        with patch.object(
            main,
            "_sync_feishu_monthly_impl",
            AsyncMock(return_value={"status": "no_cache", "seller_id": 2378517428}),
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.report_sync_feishu_monthly(
                    2378517428,
                    "2026-08",
                    BackgroundTasks(),
                    commit=True,
                )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_meitong_business_error_is_non_2xx(self):
        with patch("app.meitong_cost.run", return_value={"status": "error", "msg": "upstream failed"}):
            with self.assertRaises(HTTPException) as raised:
                with patch.object(ml_close, "invalidate_ab_verification", AsyncMock()):
                    await main.sync_meitong_cost("month_2026-08", commit=True)
        self.assertEqual(raised.exception.status_code, 502)

    async def test_unified_commit_requires_current_ab_verification(self):
        with (
            patch.object(
                ml_close,
                "audit",
                AsyncMock(return_value={
                    "state": "财务已确认终稿",
                    "ab_verified": False,
                    "report_hash": "changed-live-version",
                }),
            ),
            patch("app.unified_report.generate", AsyncMock()) as generate,
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.ml_unified_monthly(period="month_2026-08", commit=True)

        self.assertEqual(raised.exception.status_code, 409)
        generate.assert_not_awaited()

    async def test_unified_commit_binds_generator_and_postcheck_to_live_hash(self):
        approved = {
            "state": "财务已确认终稿",
            "ab_verified": True,
            "report_hash": "approved-live-hash",
        }
        generator = AsyncMock(return_value={"status": "ok", "mode": "commit"})
        with (
            patch.object(ml_close, "audit", AsyncMock(side_effect=[approved, approved])) as audit,
            patch("app.unified_report.generate", generator),
        ):
            result = await main.ml_unified_monthly(period="month_2026-08", commit=True)

        self.assertEqual("ok", result["status"])
        generator.assert_awaited_once_with(
            "month_2026-08",
            commit=True,
            expected_source_hash="approved-live-hash",
        )
        self.assertEqual(2, audit.await_count)

    async def test_operating_commit_requires_frozen_current_hash(self):
        live = {
            "state": "财务已确认暂结",
            "ab_verified": False,
            "report_hash": "live-v2",
        }
        gate = {
            "operating_close_confirmed": True,
            "operating_report_hash": "frozen-v1",
        }
        with (
            patch.object(ml_close, "audit", AsyncMock(return_value=live)),
            patch.object(ml_close, "status_endpoint", AsyncMock(return_value=gate)),
            patch("app.unified_report.generate", AsyncMock()) as generate,
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.ml_unified_monthly(
                    period="month_2026-08", commit=True, close_mode="operating"
                )

        self.assertEqual(raised.exception.status_code, 409)
        generate.assert_not_awaited()

    async def test_operating_commit_uses_separate_frozen_report(self):
        live = {
            "state": "财务已确认暂结",
            "ab_verified": False,
            "report_hash": "frozen-v1",
        }
        gate = {
            "operating_close_confirmed": True,
            "operating_report_hash": "frozen-v1",
        }
        generator = AsyncMock(return_value={"status": "ok", "mode": "commit"})
        with (
            patch.object(ml_close, "audit", AsyncMock(side_effect=[live, live])),
            patch.object(ml_close, "status_endpoint", AsyncMock(return_value=gate)),
            patch("app.unified_report.generate", generator),
        ):
            result = await main.ml_unified_monthly(
                period="month_2026-08", commit=True, close_mode="operating"
            )

        self.assertEqual("ok", result["status"])
        generator.assert_awaited_once_with(
            "month_2026-08",
            commit=True,
            expected_source_hash="frozen-v1",
            close_mode="operating",
        )

    def test_clean_month_can_enter_operating_close_before_final_ab(self):
        self.assertEqual(
            ml_close._close_state(True, False, "退回重算", ""),
            ("待运营确认", "ops_operating"),
        )
        self.assertEqual(
            ml_close._close_state(True, True, "退回重算", ""),
            ("成本缺失待补", "cost_gap"),
        )
        self.assertEqual(
            ml_close._close_state(True, False, "退回重算", "", ab_verified=True),
            ("待运营确认", "ops_final"),
        )
        self.assertEqual(
            ml_close._close_state(True, False, "", "", ab_verified=False),
            ("待运营确认", "ops_operating"),
        )
        self.assertEqual(
            ml_close._close_state(True, False, "财务已确认暂结", "", ab_verified=False),
            ("财务已确认暂结", "none"),
        )
        self.assertEqual(
            ml_close._close_state(True, False, "财务已确认暂结", "", ab_verified=True),
            ("待最终核销", "ops_final"),
        )

    def test_ab_verification_is_persisted_unless_explicitly_revoked(self):
        prior = {"最后结果JSON": '{"ab_verified": true, "report_hash": "v1", "ab_report_hash": "v1"}'}
        self.assertTrue(ml_close._resolve_ab_verified(prior, None, "v1"))
        self.assertFalse(ml_close._resolve_ab_verified(prior, None, "v2"))
        self.assertFalse(ml_close._resolve_ab_verified(prior, False, "v1"))
        self.assertTrue(ml_close._resolve_ab_verified({}, True, "v1"))
        self.assertFalse(ml_close._resolve_ab_verified({}, True, ""))


if __name__ == "__main__":
    unittest.main()
