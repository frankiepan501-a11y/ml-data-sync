import copy
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import cbt_ingest, main, lingxing, advertising, db, ml_close
from tests.test_cbt_ingest import _orders_file, _ads_file, _bill_file, _file
from tests import test_advertising_safety as ads_tests


class SeptemberSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p=patch.object(db,'DB_PATH',str(Path(self.temp.name)/'test.db'))
        p.start(); self.addCleanup(p.stop)
        await db.init_db()

    async def test_cbt_uses_monthly_usd_rate_without_explicit_override(self):
        files = [_file('Orders.xlsx', 'orders', '1'), _file('report-pads.xlsx', 'ads', '1'), _file('BILL.xlsx', 'bill', '1')]
        content = {'orders': _orders_file('2026-09', 'S'), 'ads': _ads_file('2026-09', 'S'), 'bill': _bill_file('2026-09')}
        with (
            patch.object(cbt_ingest, '_ft', AsyncMock(return_value='t')),
            patch.object(cbt_ingest, '_list_folder', AsyncMock(return_value=files)),
            patch.object(cbt_ingest, '_download', AsyncMock(side_effect=lambda t,k: content[k])),
            patch.object(lingxing, 'fetch_all_products', AsyncMock(return_value={})),
            patch.object(lingxing, 'fetch_fx_rate', AsyncMock(return_value={'USD': 6.7809})) as fx,
        ):
            result = await cbt_ingest.run('2026-09', folder_token='folder')
        self.assertEqual(6.7809, result['fx'])
        fx.assert_awaited_once_with('2026-09')

    async def test_native_partial_refund_needs_no_shipment_to_allocate(self):
        row = copy.deepcopy(ads_tests.MonthlySyncSafetyTests._cached_order())
        o = row['_payload']
        o.update(status='partially_refunded', paid_amount=222.26, total_amount=246.96)
        o['order_items'][0].update(unit_price=246.96, discounts=[{'amounts':{'seller':10}}])
        o['payments']=[{'currency_id':'MXN', 'transaction_amount_refunded':24.70}]
        with (
            patch.object(db,'cache_list_orders_for_scope',AsyncMock(return_value=[row])),
            patch.object(lingxing,'fetch_all_products',AsyncMock(return_value=ads_tests.MonthlySyncSafetyTests._products())),
            patch.object(lingxing,'fetch_fx_rate',AsyncMock(return_value={'MXN':0.4})),
            patch.object(advertising,'fetch_ad_items_for_month',AsyncMock(return_value=[])),
            patch.object(advertising,'attribute_ad_metrics_by_item_id',AsyncMock(return_value=({}, {}, []))),
            patch.object(advertising,'fetch_shop_visits_for_month',AsyncMock(return_value=0)),
            patch('app.billing.fetch_month_adjustments',AsyncMock(return_value={'unclassified_count':0})),
        ):
            result=await main._sync_feishu_monthly_impl(3383185411,'2026-07',commit=False)
        self.assertEqual(24.7,result['refund_local_total'])

    async def test_unchanged_cbt_does_not_revoke_approval(self):
        with (
            patch.object(cbt_ingest,'run',AsyncMock(return_value={'status':'ok','unchanged':True})),
            patch.object(ml_close,'invalidate_ab_verification',AsyncMock()) as revoke,
        ):
            await main.cbt_ingest('2026-09', commit=True, fx=6.7809)
        revoke.assert_not_awaited()
