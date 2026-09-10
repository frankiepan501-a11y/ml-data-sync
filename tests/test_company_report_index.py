import copy
import unittest
from unittest.mock import AsyncMock, patch
from app import company_report_index as index
from app import ml_close


class IndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_failure_restores_index_and_reports_rollback_failure(self):
        fields = {'报表链接': 'https://u1wpma3xuhr.feishu.cn/wiki/frozen', '最后结果JSON': '{}'}
        for rollback_fails in [False, True]:
            with (
                self.subTest(rollback_fails=rollback_fails),
                patch.object(index, 'publish', AsyncMock(return_value={'record_id': 'r'})),
                patch.object(index, 'rollback', AsyncMock(side_effect=RuntimeError('rollback failed') if rollback_fails else None)) as restore,
                patch.object(ml_close, '_upsert_status', AsyncMock(side_effect=RuntimeError('status failed'))),
            ):
                with self.assertRaisesRegex(RuntimeError, 'rollback failed' if rollback_fails else 'status failed'):
                    await ml_close._complete_finance_with_index('month_2026-08', fields, 'token', 'operating')
                restore.assert_awaited_once()

    async def test_rollback_does_not_overwrite_later_link(self):
        reader = AsyncMock(return_value={'data': {'record': {'fields': {index.FIELD: {'link': 'newer'}}}}})
        with self.assertRaisesRegex(RuntimeError, '其他操作'):
            await index.rollback({'record_id': 'r', 'current': {'link': 'ours'}, 'previous': None}, 'token', reader)
        self.assertEqual(reader.await_count, 1)

    async def test_write_verify_retry_and_final_upgrade(self):
        record = {'record_id': 'r', 'fields': {'日期': '2026/08', '其他平台': 'untouched'}}
        writes = []
        async def request(method, url, token, payload=None):
            if '/fields?' in url:
                return {'data': {'items': [{'field_name': index.FIELD, 'type': 15}]}}
            if '/records?' in url:
                return {'data': {'items': [copy.deepcopy(record)], 'has_more': False}}
            if method == 'PUT':
                writes.append(payload)
                record['fields'].update(payload['fields'])
            return {'data': {'record': copy.deepcopy(record)}}
        url = 'https://u1wpma3xuhr.feishu.cn/wiki/frozen'
        first = await index.publish('month_2026-08', url, 'operating', 'token', request)
        self.assertTrue(first['verified'])
        await index.publish('month_2026-08', url, 'operating', 'token', request)
        self.assertEqual(len(writes), 1)
        self.assertEqual(set(writes[0]['fields']), {index.FIELD})
        self.assertEqual(record['fields']['其他平台'], 'untouched')
        await index.publish('month_2026-08', url, 'final', 'token', request)
        with self.assertRaisesRegex(RuntimeError, '禁止降级'):
            await index.publish('month_2026-08', url, 'operating', 'token', request)

    async def test_missing_duplicate_and_readback_failure_block(self):
        for count in [0, 2, 1]:
            with self.subTest(count=count):
                async def request(method, url, token, payload=None):
                    if '/fields?' in url:
                        return {'data': {'items': [{'field_name': index.FIELD, 'type': 15}]}}
                    record = {'record_id': 'r', 'fields': {'日期': '2026/08'}}
                    if '/records?' in url:
                        return {'data': {'items': [record] * count, 'has_more': False}}
                    return {'data': {'record': record}}
                with self.assertRaises(RuntimeError):
                    await index.publish('month_2026-08', 'https://u1wpma3xuhr.feishu.cn/wiki/frozen', 'operating', 'token', request)

    async def test_review_cannot_publish(self):
        with self.assertRaises(ValueError):
            await index.publish('month_2026-08', 'https://u1wpma3xuhr.feishu.cn/wiki/frozen', 'review', 'token', None)
