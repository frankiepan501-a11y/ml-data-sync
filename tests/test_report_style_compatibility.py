import unittest
from unittest.mock import AsyncMock, patch
from app import unified_report as u


class ReportStyleCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_verified_formatters_and_inherit_exchange_format(self):
        request = AsyncMock()
        with patch.object(u, '_api_json', request):
            await u._style_report('token', 'sheet', 'main', 'source', 'check', 68)
        styles = request.call_args.args[3]['data']
        formats = {s['style']['formatter'] for s in styles if 'formatter' in s['style']}
        self.assertEqual({'#,##0.00', '0.00%', '0'}, formats)
        self.assertFalse(any(s['ranges'].startswith('main!Y2:') for s in styles))
