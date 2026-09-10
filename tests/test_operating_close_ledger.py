"""Exercise the actual confirmation handler with real SQLite publication guards."""
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from app import db, ml_close, unified_report, company_report_index


class OperatingLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_operating_confirmation_checks_operating_generation(self):
        for mode, complete, index_fails, expected in [('operating', True, False, 'ok'), ('final', True, False, 'blocked'), ('operating', False, False, 'blocked'), ('operating', True, True, 'blocked')]:
            with self.subTest(mode=mode, complete=complete), tempfile.TemporaryDirectory() as directory:
                current = {'record_id': 'r', 'fields': {'状态': '运营已确认', '最后卡片 message_id': 'card', '最后结果JSON': json.dumps({'review_source_hash': 'v3', 'review_report_url': 'https://example.test/review'})}}
                summary = {'period': 'month_2026-08', 'month': '2026-08', 'status': 'ok', 'state': '运营已确认', 'next_card': 'finance_operating', 'report_hash': 'v3', 'operating_ready': True, 'ab_verified': False}
                async def generate(period, **kwargs):
                    self.assertEqual(kwargs['close_mode'], 'operating')
                    key = unified_report._report_identity(period, mode)
                    await db.claim_unified_report_generation(key, 'generator', 'content')
                    if complete:
                        await db.complete_unified_report_generation(key, 'generator', 'content')
                    return {'status': 'ok', 'content_hash': 'content', 'url': 'https://example.test/frozen', 'spreadsheet_token': 'frozen'}
                writer = AsyncMock()
                with patch.object(db, 'DB_PATH', os.path.join(directory, 'test.db')):
                    await db.init_db()
                    with (
                        patch.object(company_report_index, 'publish', AsyncMock(return_value={'verified': True}, side_effect=RuntimeError('index write failed') if index_fails else None)),
                        patch.object(ml_close, '_tenant_token', AsyncMock(return_value='test')),
                        patch.object(ml_close, '_get_status', AsyncMock(return_value=current)),
                        patch.object(ml_close, '_upsert_status', writer),
                        patch.object(ml_close, '_open_ad_failures', AsyncMock(return_value=[])),
                        patch.object(ml_close, 'audit', AsyncMock(return_value=summary)),
                        patch.object(ml_close, 'patch_or_fallback', AsyncMock(return_value={})),
                        patch.object(unified_report, 'generate', generate),
                    ):
                        result = await ml_close.confirm_action({'action': 'ml_profit_finance_operating_confirm', 'period': 'month_2026-08', 'message_id': 'card', 'operator_id': 'finance-test', 'review_source_hash': 'v3'})
                    self.assertEqual(result['status'], expected, result.get('reason'))
                    writes = [x.args[1] for x in writer.await_args_list if x.args[1].get('状态') == '财务已确认暂结']
                    self.assertEqual(len(writes), int(expected == 'ok'))
