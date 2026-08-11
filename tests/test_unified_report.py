import unittest
from unittest.mock import AsyncMock, patch

from app import ml_close, unified_report


def _record(**fields):
    return {"record_id": fields.pop("record_id", "rec-1"), "fields": fields}


def _source_row(sku="FF01A-01", period="month_2026-08"):
    return _record(
        record_id="rec-report",
        **{
            "SKU": sku,
            "周期": period,
            "店铺": "ML 本土3店 DISTRIBUIDOR VALMIGOZ",
            "币种": "MXN",
            "件数": 2,
            "订单数": 1,
            "营收(原币)": 1000,
            "退款金额(原币)": 10,
            "ML佣金(原币)": 100,
            "物流费(原币)": 50,
            "广告费(原币)": 20,
            "VAT估算(原币)": 5,
            "我的汇率": 0.4,
            "Full仓储费(RMB)": 4,
            "采购成本(RMB)": 80,
            "头程成本(RMB)": 8,
            "海外仓成本(RMB)": 2,
            "营收(RMB)": 400,
            "退款金额(RMB)": 4,
            "ML佣金(RMB)": 40,
            "物流费(RMB)": 20,
            "广告费(RMB)": 8,
            "VAT估算(RMB)": 2,
            "全额毛利(RMB)": 232,
        },
    )


class ProductMappingTests(unittest.TestCase):
    def test_exact_maintenance_erp_sku_wins(self):
        maintenance = [_record(**{
            "ERP SKU": " ff01a‐01 ",
            "ERP品名": "YS11 Pro 手柄-涂鸦",
            "产品类型": "游戏手柄",
        })]
        costs = [_record(**{
            "ERP SKU": "FF01A-01",
            "ERP品名": "旧名称",
            "三级分类": "旧分类",
        })]

        mapped = unified_report.resolve_product_mappings(["FF01A-01"], maintenance, costs)

        self.assertEqual("YS11 Pro 手柄-涂鸦", mapped["FF01A-01"]["product_name"])
        self.assertEqual("游戏手柄", mapped["FF01A-01"]["category"])
        self.assertEqual("产品信息维护表.ERP SKU", mapped["FF01A-01"]["source"])

    def test_cost_table_erp_sku_then_distributor_sku_fallback(self):
        costs = [
            _record(record_id="cost-1", **{
                "ERP SKU": "TZ03",
                "ERP品名": "小黑包套装",
                "三级分类": "收纳包",
            }),
            _record(record_id="cost-2", **{
                "ERP SKU": "PPPJ01",
                "分销报价单SKU（Model No. ）": "OLD-PROTECTOR",
                "ERP品名": "钢化膜三片装",
                "三级分类": "钢化膜",
            }),
        ]

        mapped = unified_report.resolve_product_mappings(
            ["TZ03", "old–protector"], [], costs
        )

        self.assertEqual("产品采购成本台.ERP SKU", mapped["TZ03"]["source"])
        self.assertEqual(
            "产品采购成本台.分销报价单SKU（Model No. ）",
            mapped["old–protector"]["source"],
        )

    def test_higher_priority_conflict_blocks_instead_of_falling_through(self):
        maintenance = [
            _record(record_id="m-1", **{
                "ERP SKU": "FF01A-01", "ERP品名": "名称A", "产品类型": "手柄"
            }),
            _record(record_id="m-2", **{
                "ERP SKU": "FF01A-01", "ERP品名": "名称B", "产品类型": "手柄"
            }),
        ]
        costs = [_record(**{
            "ERP SKU": "FF01A-01", "ERP品名": "成本台名称", "三级分类": "手柄"
        })]

        with self.assertRaises(unified_report.ProductMappingError) as caught:
            unified_report.resolve_product_mappings(["FF01A-01"], maintenance, costs)

        self.assertEqual("conflicting_name", caught.exception.issues[0]["reason"])
        self.assertEqual("产品信息维护表.ERP SKU", caught.exception.issues[0]["source"])

    def test_blank_or_unmatched_mapping_blocks_report(self):
        maintenance = [_record(**{
            "ERP SKU": "FF01A-01", "ERP品名": "手柄", "产品类型": ""
        })]

        with self.assertRaises(unified_report.ProductMappingError) as caught:
            unified_report.resolve_product_mappings(
                ["FF01A-01", "UNKNOWN"], maintenance, []
            )

        self.assertEqual(
            {"missing_category", "unmatched"},
            {issue["reason"] for issue in caught.exception.issues},
        )


class WorkbookBuildTests(unittest.TestCase):
    def test_builds_approved_47_columns_and_erp_display_fields(self):
        prepared = unified_report.prepare_report(
            "month_2026-08",
            [_source_row()],
            [_record(**{
                "ERP SKU": "FF01A-01",
                "ERP品名": "YS11 Pro 手柄-涂鸦",
                "产品类型": "游戏手柄",
            })],
            [],
        )

        self.assertEqual(47, len(prepared["main_values"][0]))
        self.assertEqual(unified_report.REPORT_HEADERS, prepared["main_values"][0])
        row = prepared["main_values"][1]
        self.assertEqual("YS11 Pro 手柄-涂鸦", row[5])
        self.assertEqual("游戏手柄", row[6])
        self.assertEqual({"type": "formula", "text": "=ROUND(L2*Y2,2)"}, row[25])
        self.assertEqual({"type": "formula", "text": "=IFERROR(AL2/Z2,0)"}, row[45])
        self.assertEqual(49, len(prepared["source_values"][0]))
        self.assertEqual(1, prepared["summary"]["report_rows"])
        self.assertEqual(1, prepared["summary"]["unique_skus"])

class ReportGenerationAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapping_failure_happens_before_any_wiki_copy(self):
        with (
            patch.object(unified_report, "_report_token", AsyncMock(return_value="token")),
            patch.object(unified_report, "_find_existing_report", AsyncMock(return_value=None)),
            patch.object(unified_report, "_load_sources", AsyncMock(return_value=(
                [_source_row(sku="UNKNOWN")], [], []
            ))),
            patch.object(unified_report, "_copy_or_resume_report", AsyncMock()) as copier,
        ):
            with self.assertRaises(unified_report.ProductMappingError):
                await unified_report.generate("month_2026-08", commit=True)
            copier.assert_not_awaited()

    async def test_preview_never_copies_wiki_report(self):
        prepared = {
            "summary": {"period": "month_2026-08", "month": "2026-08", "report_rows": 1},
            "main_values": [unified_report.REPORT_HEADERS],
            "source_values": [unified_report.SOURCE_HEADERS + ["record_id"]],
            "check_values": [],
        }
        with (
            patch.object(unified_report, "_load_sources", AsyncMock(return_value=([], [], []))),
            patch.object(unified_report, "prepare_report", return_value=prepared),
            patch.object(unified_report, "_copy_or_resume_report", AsyncMock()) as copier,
        ):
            result = await unified_report.generate("month_2026-08", commit=False)

        self.assertEqual("preview", result["mode"])
        copier.assert_not_awaited()

    async def test_complete_marker_makes_commit_idempotent(self):
        existing = {
            "complete": True,
            "wiki_token": "wiki-existing",
            "spreadsheet_token": "sheet-existing",
            "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-existing",
        }
        with (
            patch.object(unified_report, "_report_token", AsyncMock(return_value="token")),
            patch.object(unified_report, "_find_existing_report", AsyncMock(return_value=existing)),
            patch.object(unified_report, "_load_sources", AsyncMock()) as loader,
            patch.object(unified_report, "_write_report", AsyncMock()) as writer,
        ):
            result = await unified_report.generate("month_2026-08", commit=True)

        self.assertTrue(result["deduped"])
        self.assertEqual("sheet-existing", result["spreadsheet_token"])
        loader.assert_not_awaited()
        writer.assert_not_awaited()


class FinanceConfirmationGeneratorGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clean_summary = {
            "status": "ok",
            "period": "month_2026-08",
            "month": "2026-08",
            "state": "运营已确认",
            "next_card": "finance_final",
            "last_error": "",
        }
        self.current = {"record_id": "status-1", "fields": {"状态": "运营已确认"}}

    async def test_generator_failure_blocks_finance_final_state(self):
        writer = AsyncMock(return_value={"record_id": "status-1"})
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=self.current)),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=self.clean_summary)),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(
                unified_report,
                "generate",
                AsyncMock(side_effect=RuntimeError("ERP品名映射失败")),
            ),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_finance_confirm",
                "period": "month_2026-08",
                "message_id": "om-finance",
                "operator_name": "财务",
            })

        self.assertEqual("blocked", result["status"])
        self.assertIn("月报生成失败", result["reason"])
        final_writes = [c.args[1] for c in writer.await_args_list]
        self.assertFalse(any(f.get("状态") == "财务已确认终稿" for f in final_writes))
        self.assertTrue(any(f.get("状态") == "异常" for f in final_writes))

    async def test_finance_state_is_written_only_after_report_succeeds(self):
        writes = []

        async def write_status(period, fields, tok=None):
            writes.append(dict(fields))
            return {"record_id": "status-1"}

        generated = {
            "status": "ok",
            "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-new",
            "wiki_token": "wiki-new",
            "spreadsheet_token": "sheet-new",
        }
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=self.current)),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=self.clean_summary)),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", AsyncMock(return_value=generated)),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_finance_confirm",
                "period": "month_2026-08",
                "message_id": "om-finance",
                "operator_name": "财务",
            })

        self.assertEqual("财务已确认终稿", result["state"])
        self.assertEqual(generated, result["report"])
        self.assertEqual("财务已确认终稿", writes[-1]["状态"])
        self.assertEqual(generated["url"], writes[-1]["报表链接"])


if __name__ == "__main__":
    unittest.main()
