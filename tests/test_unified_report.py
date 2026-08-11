import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from app import db, ml_close, unified_report


@asynccontextmanager
async def _ready_finalization_guard(period, content_hash):
    yield True


@asynccontextmanager
async def _ready_action_guard(
    period,
    action_key,
    owner,
    required_report_hash=None,
    complete_on_success=True,
):
    yield True
    if complete_on_success:
        await db.complete_ml_close_action(period, action_key, owner)


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


class WorkbookBuildTests(unittest.IsolatedAsyncioTestCase):
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

    def test_formula_readback_mismatch_blocks_completion(self):
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
        main_read = [list(row) for row in prepared["main_values"]]
        main_read[1][25] = "not-a-formula"

        checks = [list(row) for row in prepared["check_values"]]
        checks[0][4] = "ML_UNIFIED_REPORT_V1|IN_PROGRESS|month_2026-08"
        with self.assertRaises(unified_report.ReportGenerationError) as caught:
            unified_report.validate_report_readback(
                main_read,
                prepared["main_values"],
                prepared["source_values"],
                prepared["source_values"],
                checks,
                checks,
            )

        self.assertIn("Z2", str(caught.exception))

    def test_erp_display_readback_mismatch_blocks_completion(self):
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
        main_read = [list(row) for row in prepared["main_values"]]
        main_read[1][5] = "错误品名"
        checks = [list(row) for row in prepared["check_values"]]
        checks[0][4] = "ML_UNIFIED_REPORT_V1|IN_PROGRESS|month_2026-08"

        with self.assertRaises(unified_report.ReportGenerationError) as caught:
            unified_report.validate_report_readback(
                main_read,
                prepared["main_values"],
                prepared["source_values"],
                prepared["source_values"],
                checks,
                checks,
            )

        self.assertIn("F2", str(caught.exception))

    async def test_complete_marker_is_not_written_when_content_readback_fails(self):
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
        bad_main = [list(row) for row in prepared["main_values"]]
        bad_main[1][25] = "not-a-formula"
        checks = [list(row) for row in prepared["check_values"]]
        checks[0][4] = "ML_UNIFIED_REPORT_V1|IN_PROGRESS|month_2026-08"
        report = {
            "spreadsheet_token": "sheet-token",
            "sheets": [
                {"sheetId": "main", "title": "美客多毛利报表-2026-08", "rowCount": 200},
                {"sheetId": "source", "title": "数据源", "rowCount": 200},
                {"sheetId": "checks", "title": "检查", "rowCount": 200},
            ],
        }
        writer = AsyncMock()
        with (
            patch.object(unified_report, "_write_range", writer),
            patch.object(unified_report, "_style_report", AsyncMock()),
            patch.object(
                unified_report,
                "_read_range",
                AsyncMock(side_effect=[bad_main, prepared["source_values"], checks]),
            ),
        ):
            with self.assertRaises(unified_report.ReportGenerationError):
                await unified_report._write_report("token", report, prepared)

        complete_writes = [
            call for call in writer.await_args_list
            if call.args[2].endswith("!E1:E1")
            and "|COMPLETE|" in str(call.args[3])
        ]
        self.assertEqual([], complete_writes)

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
        sources = (
            [_source_row()],
            [_record(**{
                "ERP SKU": "FF01A-01",
                "ERP品名": "YS11 Pro 手柄-涂鸦",
                "产品类型": "游戏手柄",
            })],
            [],
        )
        prepared = unified_report.prepare_report("month_2026-08", *sources)
        existing = {
            "complete": True,
            "content_hash": unified_report.report_content_hash(prepared),
            "wiki_token": "wiki-existing",
            "spreadsheet_token": "sheet-existing",
            "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-existing",
        }
        with (
            patch.object(unified_report, "_report_token", AsyncMock(return_value="token")),
            patch.object(unified_report, "_find_existing_report", AsyncMock(return_value=existing)),
            patch.object(unified_report, "_load_sources", AsyncMock(return_value=sources)) as loader,
            patch.object(
                db,
                "claim_unified_report_generation",
                AsyncMock(return_value={
                    "claimed": False,
                    "status": "complete",
                    "content_hash": existing["content_hash"],
                }),
            ),
            patch.object(unified_report, "_write_report", AsyncMock()) as writer,
        ):
            result = await unified_report.generate("month_2026-08", commit=True)

        self.assertTrue(result["deduped"])
        self.assertEqual("sheet-existing", result["spreadsheet_token"])
        loader.assert_awaited_once()
        writer.assert_not_awaited()

    async def test_changed_source_rewrites_existing_generated_report_in_place(self):
        sources = (
            [_source_row()],
            [_record(**{
                "ERP SKU": "FF01A-01",
                "ERP品名": "YS11 Pro 手柄-涂鸦",
                "产品类型": "游戏手柄",
            })],
            [],
        )
        existing = {
            "complete": True,
            "content_hash": "old-hash",
            "wiki_token": "wiki-existing",
            "spreadsheet_token": "sheet-existing",
            "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-existing",
        }
        verification = {"content_hash": "new-hash", "marker": "complete"}
        with (
            patch.object(unified_report, "_report_token", AsyncMock(return_value="token")),
            patch.object(unified_report, "_find_existing_report", AsyncMock(return_value=existing)),
            patch.object(unified_report, "_load_sources", AsyncMock(return_value=sources)),
            patch.object(
                db,
                "claim_unified_report_generation",
                AsyncMock(return_value={"claimed": True, "status": "generating"}),
            ),
            patch.object(db, "set_unified_report_target", AsyncMock()),
            patch.object(db, "complete_unified_report_generation", AsyncMock()),
            patch.object(db, "fail_unified_report_generation", AsyncMock()),
            patch.object(unified_report, "_copy_or_resume_report", AsyncMock()) as copier,
            patch.object(unified_report, "_write_report", AsyncMock(return_value=verification)) as writer,
        ):
            result = await unified_report.generate("month_2026-08", commit=True)

        self.assertFalse(result["deduped"])
        self.assertEqual("wiki-existing", result["wiki_token"])
        copier.assert_not_awaited()
        writer.assert_awaited_once()

    async def test_durable_claim_blocks_second_process_before_wiki_copy(self):
        with (
            patch.object(unified_report, "_report_token", AsyncMock(return_value="token")),
            patch.object(unified_report, "_find_existing_report", AsyncMock(return_value=None)),
            patch.object(unified_report, "_load_sources", AsyncMock(return_value=(
                [_source_row()],
                [_record(**{
                    "ERP SKU": "FF01A-01",
                    "ERP品名": "YS11 Pro 手柄-涂鸦",
                    "产品类型": "游戏手柄",
                })],
                [],
            ))),
            patch.object(
                db,
                "claim_unified_report_generation",
                AsyncMock(return_value={"claimed": False, "status": "generating"}),
            ),
            patch.object(unified_report, "_copy_or_resume_report", AsyncMock()) as copier,
        ):
            with self.assertRaises(unified_report.ReportGenerationError) as caught:
                await unified_report.generate("month_2026-08", commit=True)

        self.assertIn("正在生成", str(caught.exception))
        copier.assert_not_awaited()

    async def test_sqlite_complete_without_wiki_marker_fails_closed(self):
        sources = (
            [_source_row()],
            [_record(**{
                "ERP SKU": "FF01A-01",
                "ERP品名": "YS11 Pro 手柄-涂鸦",
                "产品类型": "游戏手柄",
            })],
            [],
        )

        async def completed_claim(period, owner, expected_hash):
            return {
                "claimed": False,
                "status": "complete",
                "content_hash": expected_hash,
                "wiki_token": "deleted-wiki",
                "spreadsheet_token": "deleted-sheet",
                "url": "https://example.invalid/deleted",
            }

        with (
            patch.object(unified_report, "_report_token", AsyncMock(return_value="token")),
            patch.object(unified_report, "_find_existing_report", AsyncMock(return_value=None)),
            patch.object(unified_report, "_load_sources", AsyncMock(return_value=sources)),
            patch.object(db, "claim_unified_report_generation", side_effect=completed_claim),
            patch.object(unified_report, "_copy_or_resume_report", AsyncMock()) as copier,
        ):
            with self.assertRaises(unified_report.ReportGenerationError) as caught:
                await unified_report.generate("month_2026-08", commit=True)

        self.assertIn("不一致", str(caught.exception))
        copier.assert_not_awaited()


class PersistentGenerationClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_owner_claims_and_failure_can_be_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                first = await db.claim_unified_report_generation("month_2026-08", "owner-1")
                second = await db.claim_unified_report_generation("month_2026-08", "owner-2")
                await db.fail_unified_report_generation("month_2026-08", "owner-1", "boom")
                retry = await db.claim_unified_report_generation("month_2026-08", "owner-2")

        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual("generating", second["status"])
        self.assertTrue(retry["claimed"])

    async def test_ad_failure_cancels_an_active_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_unified_report_generation("month_2026-08", "owner-1")
                await db.set_ad_sync_failure(
                    "month_2026-08",
                    "ML 本土3店 DISTRIBUIDOR VALMIGOZ",
                    "API 429",
                )
                state = await db.get_unified_report_generation("month_2026-08")
                async with db.unified_report_finalization_guard(
                    "month_2026-08", "unused-hash"
                ) as ready:
                    finalization_ready = ready

        self.assertEqual("cancelled", state["status"])
        self.assertFalse(finalization_ready)

    async def test_changed_content_reclaims_a_completed_month(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_unified_report_generation("month_2026-08", "owner-1", "old-hash")
                await db.set_unified_report_target(
                    "month_2026-08", "owner-1", "wiki", "sheet", "https://example.invalid/wiki"
                )
                await db.complete_unified_report_generation("month_2026-08", "owner-1", "old-hash")
                retry = await db.claim_unified_report_generation(
                    "month_2026-08", "owner-2", "new-hash"
                )

        self.assertTrue(retry["claimed"])
        self.assertEqual("generating", retry["status"])

    async def test_finalization_guard_orders_cancel_after_final_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_unified_report_generation("month_2026-08", "owner-1", "hash")
                await db.set_unified_report_target(
                    "month_2026-08", "owner-1", "wiki", "sheet", "https://example.invalid/wiki"
                )
                await db.complete_unified_report_generation("month_2026-08", "owner-1", "hash")
                async with db.unified_report_finalization_guard("month_2026-08", "hash") as ready:
                    self.assertTrue(ready)
                    cancel_task = asyncio.create_task(
                        db.cancel_unified_report_generation("month_2026-08", "reject")
                    )
                    await asyncio.sleep(0.05)
                    self.assertFalse(cancel_task.done())
                await cancel_task
                state = await db.get_unified_report_generation("month_2026-08")

        self.assertEqual("cancelled", state["status"])

    async def test_finalization_guard_orders_ad_failure_after_final_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_unified_report_generation(
                    "month_2026-08", "owner-1", "hash"
                )
                await db.set_unified_report_target(
                    "month_2026-08",
                    "owner-1",
                    "wiki",
                    "sheet",
                    "https://example.invalid/wiki",
                )
                await db.complete_unified_report_generation(
                    "month_2026-08", "owner-1", "hash"
                )
                async with db.unified_report_finalization_guard(
                    "month_2026-08", "hash"
                ) as ready:
                    self.assertTrue(ready)
                    failure_task = asyncio.create_task(
                        db.set_ad_sync_failure(
                            "month_2026-08",
                            "ML 本土3店 DISTRIBUIDOR VALMIGOZ",
                            "API 429",
                        )
                    )
                    await asyncio.sleep(0.05)
                    self.assertFalse(failure_task.done())
                await failure_task
                state = await db.get_unified_report_generation("month_2026-08")
                failures = await db.list_ad_sync_failures("month_2026-08")

        self.assertEqual("cancelled", state["status"])
        self.assertEqual(1, len(failures))
        self.assertEqual("ML 本土3店 DISTRIBUIDOR VALMIGOZ", failures[0]["shop"])

    async def test_completed_close_action_remains_deduped_after_later_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                first = await db.claim_ml_close_action(
                    "month_2026-08", "card-a:ops", "owner-a"
                )
                await db.complete_ml_close_action(
                    "month_2026-08", "card-a:ops", "owner-a"
                )
                later = await db.claim_ml_close_action(
                    "month_2026-08", "card-b:finance", "owner-b"
                )
                await db.complete_ml_close_action(
                    "month_2026-08", "card-b:finance", "owner-b"
                )
                replay = await db.claim_ml_close_action(
                    "month_2026-08", "card-a:ops", "owner-c"
                )

        self.assertTrue(first["claimed"])
        self.assertTrue(later["claimed"])
        self.assertFalse(replay["claimed"])
        self.assertEqual("completed", replay["status"])

    async def test_failed_close_action_can_retry_with_same_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                first = await db.claim_ml_close_action(
                    "month_2026-08", "card-a:finance", "owner-a"
                )
                await db.fail_ml_close_action(
                    "month_2026-08", "card-a:finance", "owner-a", "Feishu timeout"
                )
                retry = await db.claim_ml_close_action(
                    "month_2026-08", "card-a:finance", "owner-b"
                )

        self.assertTrue(first["claimed"])
        self.assertTrue(retry["claimed"])
        self.assertEqual("owner-b", retry["owner"])

    async def test_failed_old_action_cannot_retry_after_a_newer_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_ml_close_action(
                    "month_2026-08", "card-a:confirm", "owner-a"
                )
                await db.fail_ml_close_action(
                    "month_2026-08", "card-a:confirm", "owner-a", "audit failed"
                )
                await db.claim_ml_close_action(
                    "month_2026-08", "card-b:reject", "owner-b"
                )
                retry = await db.claim_ml_close_action(
                    "month_2026-08", "card-a:confirm", "owner-retry"
                )

        self.assertFalse(retry["claimed"])
        self.assertEqual("superseded", retry["status"])

    async def test_only_latest_distinct_action_can_finalize(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_ml_close_action(
                    "month_2026-08", "card:confirm", "owner-confirm"
                )
                await db.claim_ml_close_action(
                    "month_2026-08", "card:reject", "owner-reject"
                )
                async with db.ml_close_action_finalization_guard(
                    "month_2026-08", "card:confirm", "owner-confirm"
                ) as confirm_ready:
                    self.assertFalse(confirm_ready)
                async with db.ml_close_action_finalization_guard(
                    "month_2026-08", "card:reject", "owner-reject"
                ) as reject_ready:
                    self.assertTrue(reject_ready)
                replay = await db.claim_ml_close_action(
                    "month_2026-08", "card:reject", "owner-replay"
                )

        self.assertFalse(replay["claimed"])
        self.assertEqual("completed", replay["status"])

    async def test_cost_recalc_work_is_visible_until_finished(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_ml_close_action(
                    "month_2026-08", "card:recalc", "owner-recalc"
                )
                work = await db.claim_ml_close_month_work(
                    "month_2026-08",
                    "cost_recalc",
                    "card:recalc",
                    "owner-recalc",
                )
                active = await db.get_active_ml_close_month_work("month_2026-08")
                await db.finish_ml_close_month_work(
                    "month_2026-08", "owner-recalc", "completed"
                )
                finished = await db.get_active_ml_close_month_work("month_2026-08")

        self.assertTrue(work["claimed"])
        self.assertEqual("cost_recalc", active["kind"])
        self.assertIsNone(finished)

    async def test_superseded_recalc_cannot_start_month_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ml.db")
            with patch.object(db, "DB_PATH", db_path):
                await db.init_db()
                await db.claim_ml_close_action(
                    "month_2026-08", "card:recalc", "owner-recalc"
                )
                await db.claim_ml_close_action(
                    "month_2026-08", "card:confirm", "owner-confirm"
                )
                work = await db.claim_ml_close_month_work(
                    "month_2026-08",
                    "cost_recalc",
                    "card:recalc",
                    "owner-recalc",
                )

        self.assertFalse(work["claimed"])
        self.assertEqual("superseded", work["status"])


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
        self.action_claim_patcher = patch.object(
            db,
            "claim_ml_close_action",
            AsyncMock(return_value={"claimed": True, "status": "processing"}),
        )
        self.action_complete_patcher = patch.object(
            db, "complete_ml_close_action", AsyncMock()
        )
        self.action_fail_patcher = patch.object(
            db, "fail_ml_close_action", AsyncMock()
        )
        self.action_discard_patcher = patch.object(
            db, "discard_ml_close_action", AsyncMock()
        )
        self.action_guard_patcher = patch.object(
            db, "ml_close_action_finalization_guard", _ready_action_guard
        )
        self.month_work_get_patcher = patch.object(
            db, "get_active_ml_close_month_work", AsyncMock(return_value=None)
        )
        self.month_work_claim_patcher = patch.object(
            db,
            "claim_ml_close_month_work",
            AsyncMock(return_value={"claimed": True, "status": "processing"}),
        )
        self.month_work_finish_patcher = patch.object(
            db, "finish_ml_close_month_work", AsyncMock()
        )
        self.action_claim = self.action_claim_patcher.start()
        self.action_complete = self.action_complete_patcher.start()
        self.action_fail = self.action_fail_patcher.start()
        self.action_discard = self.action_discard_patcher.start()
        self.action_guard_patcher.start()
        self.month_work_get = self.month_work_get_patcher.start()
        self.month_work_claim = self.month_work_claim_patcher.start()
        self.month_work_finish = self.month_work_finish_patcher.start()

    def tearDown(self):
        self.action_claim_patcher.stop()
        self.action_complete_patcher.stop()
        self.action_fail_patcher.stop()
        self.action_discard_patcher.stop()
        self.action_guard_patcher.stop()
        self.month_work_get_patcher.stop()
        self.month_work_claim_patcher.stop()
        self.month_work_finish_patcher.stop()

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
            "content_hash": "hash-new",
        }
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=self.current)),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=self.clean_summary)),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", AsyncMock(return_value=generated)),
            patch.object(
                db,
                "unified_report_finalization_guard",
                _ready_finalization_guard,
            ),
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

    async def test_external_reject_during_generation_blocks_final_state(self):
        retreated = {"record_id": "status-1", "fields": {
            "状态": "退回重算",
            "最后按钮动作Key": "other-message:ml_profit_finance_reject",
        }}
        statuses = AsyncMock(side_effect=[self.current, self.current, retreated])
        writer = AsyncMock(return_value={"record_id": "status-1"})
        generated = {
            "status": "ok",
            "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-new",
            "wiki_token": "wiki-new",
            "spreadsheet_token": "sheet-new",
            "content_hash": "hash-new",
        }
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", statuses),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value=self.clean_summary)),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", AsyncMock(return_value=generated)),
            patch.object(
                db,
                "unified_report_finalization_guard",
                _ready_finalization_guard,
            ),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_finance_confirm",
                "period": "month_2026-08",
                "message_id": "om-finance-race",
                "operator_name": "财务",
            })

        self.assertEqual("blocked", result["status"])
        self.assertIn("请先完成运营确认", result["reason"])
        final_writes = [c.args[1] for c in writer.await_args_list]
        self.assertFalse(any(fields.get("状态") == "财务已确认终稿" for fields in final_writes))

    async def test_duplicate_finance_callback_does_not_cancel_first_confirmation(self):
        period = "month_2026-09"
        payload = {
            "action": "ml_profit_finance_confirm",
            "period": period,
            "message_id": "om-duplicate-finance",
            "operator_name": "财务",
        }
        mutable_fields = {"状态": "运营已确认"}
        entered = asyncio.Event()
        release = asyncio.Event()

        async def get_status(period_value, tok=None):
            return {"record_id": "status-1", "fields": dict(mutable_fields)}

        async def write_status(period_value, fields, tok=None):
            mutable_fields.update(fields)
            return {"record_id": "status-1"}

        async def generate(period_value, commit=True):
            entered.set()
            await release.wait()
            return {
                "status": "ok",
                "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-new",
                "wiki_token": "wiki-new",
                "spreadsheet_token": "sheet-new",
                "content_hash": "hash-new",
            }

        ml_close._ACTION_EPOCHS.pop(period, None)
        ml_close._ACTION_KEY_EPOCHS.pop(period, None)
        claim_count = 0

        async def claim_action(*args, **kwargs):
            nonlocal claim_count
            claim_count += 1
            return {
                "claimed": claim_count == 1,
                "status": "processing",
            }

        with (
            patch.object(db, "claim_ml_close_action", side_effect=claim_action),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", side_effect=get_status),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value={
                **self.clean_summary,
                "period": period,
                "month": "2026-09",
            })),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", side_effect=generate),
            patch.object(db, "unified_report_finalization_guard", _ready_finalization_guard),
        ):
            first_task = asyncio.create_task(ml_close.confirm_action(payload))
            await entered.wait()
            duplicate_task = asyncio.create_task(ml_close.confirm_action(payload))
            await asyncio.sleep(0)
            release.set()
            first, duplicate = await asyncio.gather(first_task, duplicate_task)

        self.assertEqual("财务已确认终稿", first["state"])
        self.assertTrue(duplicate["deduped"])
        self.assertEqual("财务已确认终稿", mutable_fields["状态"])

    async def test_old_completed_ops_card_cannot_overwrite_finance_final(self):
        current = {
            "record_id": "status-1",
            "fields": {"状态": "财务已确认终稿"},
        }
        audit_mock = AsyncMock()
        writer = AsyncMock()
        self.action_claim.return_value = {
            "claimed": False,
            "status": "completed",
        }
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=current)),
            patch.object(ml_close, "_upsert_status", writer),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", audit_mock),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_ops_confirm",
                "period": "month_2026-08",
                "message_id": "om-old-ops",
                "operator_name": "运营",
            })

        self.assertEqual("blocked", result["status"])
        self.assertIn("财务确认终稿", result["reason"])
        self.action_claim.assert_not_awaited()
        writer.assert_not_awaited()
        audit_mock.assert_not_awaited()

    async def test_same_finance_card_retries_after_final_state_write_failure(self):
        period = "month_2026-10"
        payload = {
            "action": "ml_profit_finance_confirm",
            "period": period,
            "message_id": "om-finance-retry",
            "operator_name": "财务",
        }
        fields = {"状态": "运营已确认"}
        ledger: dict[str, str] = {}
        final_attempts = 0

        async def claim_action(period_value, action_key, owner):
            status = ledger.get(action_key)
            if status in (None, "failed"):
                ledger[action_key] = "processing"
                return {"claimed": True, "status": "processing"}
            return {"claimed": False, "status": status}

        async def complete_action(period_value, action_key, owner):
            ledger[action_key] = "completed"

        async def fail_action(period_value, action_key, owner, error):
            ledger[action_key] = "failed"

        async def get_status(period_value, tok=None):
            return {"record_id": "status-1", "fields": dict(fields)}

        async def write_status(period_value, update, tok=None):
            nonlocal final_attempts
            if update.get("状态") == "财务已确认终稿":
                final_attempts += 1
                if final_attempts == 1:
                    raise TimeoutError("Feishu timeout")
            fields.update(update)
            return {"record_id": "status-1"}

        generated = {
            "status": "ok",
            "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-new",
            "wiki_token": "wiki-new",
            "spreadsheet_token": "sheet-new",
            "content_hash": "hash-new",
        }
        ml_close._ACTION_EPOCHS.pop(period, None)
        ml_close._ACTION_KEY_EPOCHS.pop(period, None)
        with (
            patch.object(db, "claim_ml_close_action", side_effect=claim_action),
            patch.object(db, "complete_ml_close_action", side_effect=complete_action),
            patch.object(db, "fail_ml_close_action", side_effect=fail_action),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", side_effect=get_status),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", AsyncMock(return_value={
                **self.clean_summary,
                "period": period,
                "month": "2026-10",
            })),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", AsyncMock(return_value=generated)),
            patch.object(db, "unified_report_finalization_guard", _ready_finalization_guard),
        ):
            first = await ml_close.confirm_action(payload)
            second = await ml_close.confirm_action(payload)

        self.assertEqual("blocked", first["status"])
        self.assertEqual("财务已确认终稿", second["state"])
        self.assertEqual("completed", ledger["om-finance-retry:ml_profit_finance_confirm"])
        self.assertEqual(2, final_attempts)

    async def test_unhandled_audit_failure_marks_action_retryable(self):
        period = "month_2026-11"
        payload = {
            "action": "ml_profit_finance_confirm",
            "period": period,
            "message_id": "om-audit-retry",
            "operator_name": "财务",
        }
        fields = {"状态": "运营已确认"}
        ledger: dict[str, str] = {}

        async def claim_action(period_value, action_key, owner):
            status = ledger.get(action_key)
            if status in (None, "failed"):
                ledger[action_key] = "processing"
                return {"claimed": True, "status": "processing"}
            return {"claimed": False, "status": status}

        async def complete_action(period_value, action_key, owner):
            ledger[action_key] = "completed"

        async def fail_action(period_value, action_key, owner, error):
            ledger[action_key] = "failed"

        async def get_status(period_value, tok=None):
            return {"record_id": "status-1", "fields": dict(fields)}

        async def write_status(period_value, update, tok=None):
            fields.update(update)
            return {"record_id": "status-1"}

        generated = {
            "status": "ok",
            "url": "https://u1wpma3xuhr.feishu.cn/wiki/wiki-new",
            "wiki_token": "wiki-new",
            "spreadsheet_token": "sheet-new",
            "content_hash": "hash-new",
        }
        audit_calls = 0

        async def audit_once_broken(**kwargs):
            nonlocal audit_calls
            audit_calls += 1
            if audit_calls == 1:
                raise RuntimeError("audit unavailable")
            return {
                **self.clean_summary,
                "period": period,
                "month": "2026-11",
            }

        ml_close._ACTION_EPOCHS.pop(period, None)
        ml_close._ACTION_KEY_EPOCHS.pop(period, None)
        with (
            patch.object(db, "claim_ml_close_action", side_effect=claim_action),
            patch.object(db, "complete_ml_close_action", side_effect=complete_action),
            patch.object(db, "fail_ml_close_action", side_effect=fail_action),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", side_effect=get_status),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", side_effect=audit_once_broken),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", AsyncMock(return_value=generated)),
            patch.object(db, "unified_report_finalization_guard", _ready_finalization_guard),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                await ml_close.confirm_action(payload)
            self.assertEqual("failed", ledger["om-audit-retry:ml_profit_finance_confirm"])
            second = await ml_close.confirm_action(payload)

        self.assertEqual("财务已确认终稿", second["state"])
        self.assertEqual("completed", ledger["om-audit-retry:ml_profit_finance_confirm"])

    async def test_ops_reject_supersedes_concurrent_ops_confirm(self):
        period = "month_2026-12"
        message_id = "om-ops-choice"
        fields = {
            "状态": "待运营确认",
            "最后卡片 message_id": message_id,
        }
        latest_action_key = ""
        audit_entered = asyncio.Event()
        release_confirm_audit = asyncio.Event()
        audit_calls = 0
        writes: list[dict] = []

        async def claim_action(period_value, action_key, owner):
            nonlocal latest_action_key
            latest_action_key = action_key
            return {"claimed": True, "status": "processing"}

        @asynccontextmanager
        async def latest_action_guard(period_value, action_key, owner):
            yield action_key == latest_action_key

        async def get_status(period_value, tok=None):
            return {"record_id": "status-1", "fields": dict(fields)}

        async def write_status(period_value, update, tok=None):
            writes.append(dict(update))
            fields.update(update)
            return {"record_id": "status-1"}

        async def staged_audit(**kwargs):
            nonlocal audit_calls
            audit_calls += 1
            if audit_calls == 1:
                audit_entered.set()
                await release_confirm_audit.wait()
            return {
                "status": "ok",
                "period": period,
                "month": "2026-12",
                "state": "待运营确认",
                "next_card": "ops_final",
                "last_error": "",
            }

        sender = AsyncMock(return_value={"data": {"message_id": "om-finance-new"}})
        ml_close._ACTION_EPOCHS.pop(period, None)
        ml_close._ACTION_KEY_EPOCHS.pop(period, None)
        with (
            patch.object(db, "claim_ml_close_action", side_effect=claim_action),
            patch.object(db, "complete_ml_close_action", AsyncMock()),
            patch.object(db, "fail_ml_close_action", AsyncMock()),
            patch.object(db, "cancel_unified_report_generation", AsyncMock()),
            patch.object(db, "ml_close_action_finalization_guard", latest_action_guard),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", side_effect=get_status),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", side_effect=staged_audit),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(ml_close, "send_card", sender),
        ):
            confirm_task = asyncio.create_task(ml_close.confirm_action({
                "action": "ml_profit_ops_confirm",
                "period": period,
                "message_id": message_id,
                "operator_name": "运营",
            }))
            await audit_entered.wait()
            reject = await ml_close.confirm_action({
                "action": "ml_profit_ops_reject",
                "period": period,
                "message_id": message_id,
                "operator_name": "运营",
            })
            release_confirm_audit.set()
            confirm = await confirm_task

        self.assertEqual("退回重算", reject["state"])
        self.assertEqual("blocked", confirm["status"])
        self.assertEqual("退回重算", fields["状态"])
        self.assertFalse(any(
            "最后按钮动作Key" in update and "状态" not in update
            for update in writes
        ))
        sender.assert_not_awaited()

    async def test_stale_reject_does_not_supersede_current_ops_confirm(self):
        period = "month_2027-02"
        current_message_id = "om-current-ops"
        stale_message_id = "om-stale-ops"
        fields = {
            "状态": "待运营确认",
            "最后卡片 message_id": current_message_id,
        }
        latest_action_key = ""
        claimed_keys: list[str] = []
        audit_entered = asyncio.Event()
        release_audit = asyncio.Event()

        async def claim_action(period_value, action_key, owner):
            nonlocal latest_action_key
            claimed_keys.append(action_key)
            latest_action_key = action_key
            return {"claimed": True, "status": "processing"}

        @asynccontextmanager
        async def latest_action_guard(
            period_value,
            action_key,
            owner,
            required_report_hash=None,
            complete_on_success=True,
        ):
            yield action_key == latest_action_key

        async def get_status(period_value, tok=None):
            return {"record_id": "status-1", "fields": dict(fields)}

        async def write_status(period_value, update, tok=None):
            fields.update(update)
            return {"record_id": "status-1"}

        async def staged_audit(**kwargs):
            audit_entered.set()
            await release_audit.wait()
            return {
                "status": "ok",
                "period": period,
                "month": "2027-02",
                "state": "待运营确认",
                "next_card": "ops_final",
                "last_error": "",
            }

        sender = AsyncMock(return_value={"data": {"message_id": "om-finance-new"}})
        ml_close._ACTION_EPOCHS.pop(period, None)
        ml_close._ACTION_KEY_EPOCHS.pop(period, None)
        with (
            patch.object(db, "claim_ml_close_action", side_effect=claim_action),
            patch.object(db, "complete_ml_close_action", AsyncMock()),
            patch.object(db, "fail_ml_close_action", AsyncMock()),
            patch.object(db, "discard_ml_close_action", AsyncMock()),
            patch.object(db, "cancel_unified_report_generation", AsyncMock()),
            patch.object(db, "ml_close_action_finalization_guard", latest_action_guard),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", side_effect=get_status),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", side_effect=staged_audit),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(ml_close, "send_card", sender),
        ):
            confirm_task = asyncio.create_task(ml_close.confirm_action({
                "action": "ml_profit_ops_confirm",
                "period": period,
                "message_id": current_message_id,
                "operator_name": "运营",
            }))
            await audit_entered.wait()
            stale_reject = await ml_close.confirm_action({
                "action": "ml_profit_ops_reject",
                "period": period,
                "message_id": stale_message_id,
                "operator_name": "运营",
            })
            release_audit.set()
            confirm = await confirm_task

        self.assertEqual("blocked", stale_reject["status"])
        self.assertIn("最新操作卡", stale_reject["reason"])
        self.assertEqual(1, len(claimed_keys))
        self.assertEqual(
            f"{current_message_id}:ml_profit_ops_confirm",
            claimed_keys[0],
        )
        self.assertEqual("运营已确认", confirm["state"])
        self.assertEqual("运营已确认", fields["状态"])
        sender.assert_awaited_once()

    async def test_newer_reject_blocks_old_recalc_status_commit(self):
        period = "month_2027-01"
        message_id = "om-recalc-choice"
        fields = {
            "状态": "待运营确认",
            "最后卡片 message_id": message_id,
        }
        latest_action_key = ""
        recalc_audit_entered = asyncio.Event()
        release_recalc_audit = asyncio.Event()
        audit_commits: list[bool] = []

        async def claim_action(period_value, action_key, owner):
            nonlocal latest_action_key
            latest_action_key = action_key
            return {"claimed": True, "status": "processing"}

        @asynccontextmanager
        async def latest_action_guard(
            period_value,
            action_key,
            owner,
            required_report_hash=None,
            complete_on_success=True,
        ):
            yield action_key == latest_action_key

        async def get_status(period_value, tok=None):
            return {"record_id": "status-1", "fields": dict(fields)}

        async def write_status(period_value, update, tok=None):
            fields.update(update)
            return {"record_id": "status-1"}

        audit_calls = 0

        async def staged_audit(**kwargs):
            nonlocal audit_calls
            audit_calls += 1
            audit_commits.append(bool(kwargs.get("commit")))
            if audit_calls == 1:
                recalc_audit_entered.set()
                await release_recalc_audit.wait()
            return {
                "status": "ok",
                "period": period,
                "month": "2027-01",
                "state": "待运营确认",
                "next_card": "ops_final",
                "last_error": "",
                "report_rows": 1,
                "gap_row_count": 0,
                "cost_summary": {"status": "ok"},
            }

        commit_snapshot = AsyncMock()
        ml_close._ACTION_EPOCHS.pop(period, None)
        ml_close._ACTION_KEY_EPOCHS.pop(period, None)
        with (
            patch.object(db, "claim_ml_close_action", side_effect=claim_action),
            patch.object(db, "complete_ml_close_action", AsyncMock()),
            patch.object(db, "fail_ml_close_action", AsyncMock()),
            patch.object(db, "cancel_unified_report_generation", AsyncMock()),
            patch.object(db, "ml_close_action_finalization_guard", latest_action_guard),
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", side_effect=get_status),
            patch.object(ml_close, "_upsert_status", side_effect=write_status),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "audit", side_effect=staged_audit),
            patch.object(ml_close, "_commit_audit_snapshot", commit_snapshot),
            patch.object(ml_close.meitong_cost, "run", return_value={"status": "ok"}),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(ml_close, "send_card", AsyncMock()),
        ):
            recalc_task = asyncio.create_task(ml_close.confirm_action({
                "action": "ml_profit_recalc_cost",
                "period": period,
                "message_id": message_id,
                "operator_name": "运营",
            }))
            await recalc_audit_entered.wait()
            reject = await ml_close.confirm_action({
                "action": "ml_profit_ops_reject",
                "period": period,
                "message_id": message_id,
                "operator_name": "运营",
            })
            release_recalc_audit.set()
            recalc = await recalc_task

        self.assertEqual("退回重算", reject["state"])
        self.assertEqual("blocked", recalc["status"])
        self.assertEqual("退回重算", fields["状态"])
        self.assertEqual(False, audit_commits[0])
        commit_snapshot.assert_not_awaited()

    async def test_invalid_finance_state_blocks_before_generator(self):
        current = {"record_id": "status-1", "fields": {"状态": "退回重算"}}
        generator = AsyncMock()
        with (
            patch.object(ml_close, "_tenant_token", AsyncMock(return_value="token")),
            patch.object(ml_close, "_get_status", AsyncMock(return_value=current)),
            patch.object(ml_close, "_upsert_status", AsyncMock()),
            patch.object(ml_close, "_open_ad_failures", AsyncMock(return_value=[])),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", generator),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_finance_confirm",
                "period": "month_2026-08",
                "message_id": "om-stale-finance",
                "operator_name": "财务",
            })

        self.assertEqual("blocked", result["status"])
        self.assertIn("请先完成运营确认", result["reason"])
        generator.assert_not_awaited()

    async def test_active_cost_recalc_blocks_finance_confirmation(self):
        self.month_work_get.return_value = {
            "period": "month_2026-08",
            "kind": "cost_recalc",
            "status": "processing",
        }
        generator = AsyncMock()
        token = AsyncMock(return_value="token")
        with (
            patch.object(ml_close, "_tenant_token", token),
            patch.object(ml_close, "patch_or_fallback", AsyncMock(return_value={})),
            patch.object(unified_report, "generate", generator),
        ):
            result = await ml_close.confirm_action({
                "action": "ml_profit_finance_confirm",
                "period": "month_2026-08",
                "message_id": "om-finance-during-recalc",
                "operator_name": "财务",
            })

        self.assertEqual("blocked", result["status"])
        self.assertIn("成本正在重算", result["reason"])
        generator.assert_not_awaited()
        token.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
