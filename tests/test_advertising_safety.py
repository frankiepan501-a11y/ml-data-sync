import copy
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import advertising, db, lingxing, main


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
                await main._sync_feishu_monthly_impl(3383185411, "2026-07")

        self.assertEqual(502, ctx.exception.status_code)
        self.assertIn("advertising", str(ctx.exception.detail).lower())
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
        ):
            result = await main._sync_feishu_monthly_impl(3383185411, "2026-07", commit=True)
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


if __name__ == "__main__":
    unittest.main()
