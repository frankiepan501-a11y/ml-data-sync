import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException

from app import main, ml_close


class MonthBackfillTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_backfill_uses_site_local_month_boundaries(self):
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
        self.assertEqual(impl.await_args.kwargs["date_from"], "2026-08-01T00:00:00.000-03:00")
        self.assertEqual(impl.await_args.kwargs["date_to"], "2026-09-01T00:00:00.000-03:00")

        with (
            patch.object(main, "_report_sku_recent_impl", AsyncMock(return_value=result)) as impl,
            patch.object(main.db, "cache_replace_month_scope", AsyncMock(return_value=0)),
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=[])),
        ):
            await main.admin_backfill_orders(3383185411, month="2026-08", refresh_after=1000)
        self.assertEqual(impl.await_args.kwargs["date_from"], "2026-08-01T00:00:00.000-06:00")
        self.assertEqual(impl.await_args.kwargs["date_to"], "2026-09-01T00:00:00.000-06:00")

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
        }
        cached = [{"order_id": 101}, {"order_id": 102}]
        with (
            patch.object(main, "_report_sku_recent_impl", AsyncMock(return_value=result)),
            patch.object(main.db, "cache_replace_month_scope", AsyncMock(return_value=2)) as replace,
            patch.object(main.db, "cache_list_orders_for_scope", AsyncMock(return_value=cached)),
        ):
            response = await main.admin_backfill_orders(2378517428, month="2026-08", refresh_after=1000)

        replace.assert_awaited_once_with(2378517428, "2026-08", [101, 102])
        self.assertEqual(response["cached_month_unique"], 2)
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
                main.sync_meitong_cost("month_2026-08", commit=True)
        self.assertEqual(raised.exception.status_code, 502)

    def test_rejected_month_stays_blocked_until_ab_is_explicitly_verified(self):
        self.assertEqual(
            ml_close._close_state(True, False, "退回重算", ""),
            ("退回重算", "none"),
        )
        self.assertEqual(
            ml_close._close_state(True, True, "退回重算", ""),
            ("退回重算", "none"),
        )
        self.assertEqual(
            ml_close._close_state(True, False, "退回重算", "", ab_verified=True),
            ("待运营确认", "ops_final"),
        )
        self.assertEqual(
            ml_close._close_state(True, False, "", "", ab_verified=False),
            ("退回重算", "none"),
        )
        self.assertEqual(
            ml_close._close_state(True, False, "待数据同步", "", ab_verified=False),
            ("退回重算", "none"),
        )

    def test_ab_verification_is_persisted_unless_explicitly_revoked(self):
        prior = {"最后结果JSON": '{"ab_verified": true}'}
        self.assertTrue(ml_close._resolve_ab_verified(prior, None))
        self.assertFalse(ml_close._resolve_ab_verified(prior, False))
        self.assertTrue(ml_close._resolve_ab_verified({}, True))


if __name__ == "__main__":
    unittest.main()
