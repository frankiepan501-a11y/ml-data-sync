import copy
import unittest
from unittest.mock import patch

from app import cbt_reconcile as c, unified_report as u


class SeptemberRevisionSafetyTests(unittest.TestCase):
    def test_old_or_incomplete_september_source_cannot_claim_verification(self):
        row = {"fields": {"店铺": c.SHOP, "周期": c.PERIOD, "SKU": "old"}}
        with self.assertRaisesRegex(u.ReportGenerationError, "完整17"):
            u.september_cbt_evidence([row])
        rows = [{"fields": {**row["fields"], "SKU": str(i), "财务修订版本": c.VERSION}} for i in range(17)]
        with self.assertRaisesRegex(u.ReportGenerationError, "实际源字段"):
            u.september_cbt_evidence(rows)
        self.assertIsNone(u.september_cbt_evidence([]))
    def test_empty_new_schema_fields_preserve_august_hash_but_values_remain_bound(self):
        old = [{"record_id": "old", "fields": {"周期": "month_2026-08", "件数": 10}}]
        added = copy.deepcopy(old)
        added[0]["fields"].update({"净销量": None, "退货数量": None})
        self.assertEqual(u.approval_source_hash(old), u.approval_source_hash(added))
        added[0]["fields"]["净销量"] = 8
        self.assertNotEqual(u.approval_source_hash(old), u.approval_source_hash(added))

    def test_changed_export_or_other_month_cannot_reuse_claim_evidence(self):
        with self.assertRaisesRegex(RuntimeError, "只适用于"):
            c.reconcile(b"", b"", b"", "2026-08")
        with self.assertRaisesRegex(RuntimeError, "导出已变更"):
            c.reconcile(b"changed export", b"", b"", "2026-09")

    def test_revision_is_scoped_to_exact_month_shop_and_version(self):
        fields = {"财务修订版本": c.VERSION, "周期": c.PERIOD, "店铺": c.SHOP,
                  "我的汇率": 6.7809, "采购成本(RMB)": 100, "头程成本(RMB)": 10}
        self.assertIsInstance(u.revision_cost_rounding_delta(fields), float)
        for key, value in (("周期", "month_2026-08"), ("店铺", "another shop"),
                           ("财务修订版本", "unverified")):
            with self.assertRaises(u.ReportGenerationError):
                u.revision_cost_rounding_delta({**fields, key: value})

    def test_separate_quantities_do_not_change_source_cost_units_or_august_layout(self):
        row = {"record_id": "sample", "fields": {"SKU": "SKU-1", "周期": c.PERIOD,
               "店铺": c.SHOP, "件数": 10, "净销量": 8, "退货数量": 1, "我的汇率": 1,
               "财务修订版本": c.VERSION, "全额毛利(RMB)": 0}}
        product = {"fields": {"ERP SKU": "SKU-1", "ERP品名": "Sample", "产品类型": "Sample"}}
        before = copy.deepcopy(row)
        with patch.object(u, 'september_cbt_evidence', return_value=None):
            prepared = u.prepare_report(c.PERIOD, [row], [product], [], close_mode="review")
        header = prepared["source_values"][0]
        self.assertEqual(10, prepared["source_values"][1][header.index("件数")])
        self.assertEqual(8, prepared["source_values"][1][header.index("净销量")])
        self.assertEqual(47, len(prepared["main_values"][0]))
        self.assertNotEqual(prepared["main_values"][1][9]["text"], "='数据源'!T2")
        self.assertEqual(before, row)
        august = copy.deepcopy(row)
        august["fields"]["周期"] = "month_2026-08"
        august["fields"].pop("财务修订版本")
        old = u.prepare_report("month_2026-08", [august], [product], [], close_mode="review")
        self.assertEqual(u.SOURCE_HEADERS+["record_id"], old["source_values"][0])

