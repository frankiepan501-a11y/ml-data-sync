import unittest
from unittest.mock import patch
from app import meitong_cost as cost


class CostSourceTests(unittest.TestCase):
    def test_meitong_error_cannot_become_empty_source(self):
        with patch.object(cost,'_http',side_effect=[{'access_token':'test'},{'code':500,'message':'failed'}]):
            with self.assertRaisesRegex(RuntimeError,'响应失败'):
                cost.meitong_orders()

    def test_erp_short_page_does_not_skip_records(self):
        with patch.object(cost,'_lx',return_value={'code':0,'total':2,'data':[{'sku':'S'}]}):
            with self.assertRaisesRegex(RuntimeError,'数量不完整'):
                cost.load_erp()

    def test_cost_preview_reads_second_page(self):
        first=[{'record_id':str(i),'fields':{'周期':'month_2026-08'}} for i in range(500)]
        last=[{'record_id':'target','fields':{'周期':'month_2026-09','店铺':'ML 本土3店','SKU':'S','件数':2}}]
        with patch.object(cost,'build_unit',return_value={('S','美客多'):(3,0)}), \
             patch.object(cost,'_fs',side_effect=[{'data':{'items':first,'has_more':True,'page_token':'p2'}},{'data':{'items':last,'has_more':False}}]):
            result=cost.run('month_2026-09',commit=False)
        self.assertEqual(1,result['rows_in_period'])
        self.assertEqual(6,result['head_total'])
        self.assertEqual('target',result['detail'][0]['record_id'])


class ConfirmedBrazilCostTests(unittest.TestCase):
    def source(self):
        header = ['送往中转仓的货件号', '物流商', '国家', '店铺', 'ERP-SKU', '数量', '单个产品头程']
        rows = [header]
        for i in range(1, 25):
            sku, qty = ('FF05A-01', 30) if i >= 20 else ('TZ06', 24) if i >= 8 else ('TZ07', 24)
            rows.append([f'10006769U{i:03}', '神龙行', '巴西', '巴西自营', sku, qty, 2619 / 24 / qty])
        rows.append(['202601-KT-54', '三沐', '巴西', '巴西自营', 'FF05A-01', 30, 30.38530438197674])
        return rows

    def test_invoice_conversion_preserves_total_and_other_carrier(self):
        rows = self.source()
        with patch.object(cost, '_sheet', return_value=rows):
            result = cost.zcol_rows()
        self.assertAlmostEqual(18087.34, sum(r['z'] * r['qty'] for r in result[:24]), places=6)
        self.assertEqual(30.38530438197674, result[-1]['z'])
        self.assertAlmostEqual(2619, sum(r[5] * r[6] for r in rows[1:25]), places=6)

    def test_already_converted_invoice_is_not_converted_again(self):
        rows = self.source()
        for r in rows[1:25]:
            r[6] *= 18087.34 / 2619
        with patch.object(cost, '_sheet', return_value=rows):
            result = cost.zcol_rows()
        self.assertAlmostEqual(18087.34, sum(r['z'] * r['qty'] for r in result[:24]), places=6)

    def test_incomplete_duplicate_changed_or_mixed_currency_batch_blocks(self):
        for change in ('missing', 'duplicate', 'quantity', 'mixed_currency', 'platform', 'nonfinite'):
            with self.subTest(change=change):
                rows = self.source()
                if change == 'missing': rows.pop(1)
                if change == 'duplicate': rows[1][0] = rows[2][0]
                if change == 'quantity': rows[1][5] = 25
                if change == 'mixed_currency': rows[1][6] *= 6.9062
                if change == 'platform': rows[1][2:4] = ['墨西哥', '美客多CBT']
                if change == 'nonfinite': rows[1][6] = float('nan')
                with patch.object(cost, '_sheet', return_value=rows):
                    with self.assertRaisesRegex(RuntimeError, '10006769 cost evidence'):
                        cost.zcol_rows()

    def test_brazil_alias_does_not_change_mexico_cost(self):
        records = [{'record_id': c, 'fields': {'周期': 'month_2026-09', '店铺': shop,
                    'SKU': 'FF05-2', '件数': 23}} for c, shop in [('br', 'ML 巴西店'), ('mx', 'ML CBT')]]
        with patch.object(cost, 'build_unit', return_value={('FF05A-01', '美客多巴西'): (25, 3),
                                                         ('FF05-2', '美客多'): (10, 0)}), \
             patch.object(cost, '_fs', return_value={'data': {'items': records, 'has_more': False}}):
            result = cost.run('month_2026-09')
        self.assertEqual([575, 230], [r['head'] for r in result['detail']])
        self.assertEqual([69, 0], [r['ovs'] for r in result['detail']])

    def test_missing_source_column_cannot_bypass_currency_guard(self):
        for col in range(7):
            with self.subTest(column=col):
                rows = self.source()
                rows[0][col] += '_renamed'
                with patch.object(cost, '_sheet', return_value=rows):
                    with self.assertRaisesRegex(RuntimeError, 'required source column missing'):
                        cost.zcol_rows()
