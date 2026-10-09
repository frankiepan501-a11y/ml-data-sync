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
