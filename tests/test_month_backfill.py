import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException

from app import main


class MonthBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_search_uses_order_prefixed_date_filter(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"results": [], "paging": {"total": 0}})
        with (
            patch.object(main.db, "get_token", AsyncMock(return_value={"access_token": "x", "app_key": "local_br"})),
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
        }
        with patch.object(main, "_report_sku_recent_impl", AsyncMock(return_value=result)) as impl:
            await main.admin_backfill_orders(2378517428, month="2026-08")
        self.assertEqual(impl.await_args.kwargs["date_from"], "2026-08-01T00:00:00.000-03:00")
        self.assertEqual(impl.await_args.kwargs["date_to"], "2026-09-01T00:00:00.000-03:00")

        with patch.object(main, "_report_sku_recent_impl", AsyncMock(return_value=result)) as impl:
            await main.admin_backfill_orders(3383185411, month="2026-08")
        self.assertEqual(impl.await_args.kwargs["date_from"], "2026-08-01T00:00:00.000-06:00")
        self.assertEqual(impl.await_args.kwargs["date_to"], "2026-09-01T00:00:00.000-06:00")

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


if __name__ == "__main__":
    unittest.main()
