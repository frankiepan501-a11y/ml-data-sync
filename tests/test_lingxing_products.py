import unittest
from unittest.mock import AsyncMock, patch

from app import lingxing


class LingxingProductTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lingxing._products_cache["items"] = None
        lingxing._products_cache["expires_at"] = 0

    async def test_empty_first_page_is_retried_and_not_cached(self):
        empty = {"code": 0, "data": [], "total": 0}
        complete = {
            "code": 0,
            "data": [{"sku": "SKU1", "cg_price": 10}],
            "total": 1,
        }

        with (
            patch.object(lingxing, "lx_api", AsyncMock(side_effect=[empty, complete])) as api,
            patch.object(lingxing.asyncio, "sleep", AsyncMock()) as sleeper,
        ):
            result = await lingxing.fetch_all_products()

        self.assertEqual({"SKU1"}, set(result))
        self.assertEqual(2, api.await_count)
        sleeper.assert_awaited_once_with(2.0)
        self.assertEqual(result, lingxing._products_cache["items"])

    async def test_api_error_code_is_retried_before_success(self):
        rejected = {"code": 2001006, "message": "temporary reject", "data": [], "total": 0}
        complete = {
            "code": 0,
            "data": [{"sku": "SKU2", "cg_price": 20}],
            "total": 1,
        }

        with (
            patch.object(lingxing, "lx_api", AsyncMock(side_effect=[rejected, complete])),
            patch.object(lingxing.asyncio, "sleep", AsyncMock()),
        ):
            result = await lingxing.fetch_all_products()

        self.assertEqual({"SKU2"}, set(result))
