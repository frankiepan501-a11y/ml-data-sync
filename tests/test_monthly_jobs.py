import tempfile
import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import BackgroundTasks

from app import db, monthly_jobs


class MonthlyJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(db, 'DB_PATH', str(Path(self.temp.name)/'test.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        await db.init_db()

    async def test_accepted_does_not_mean_success_and_failure_releases_work(self):
        bg = BackgroundTasks()
        with patch.object(monthly_jobs, '_approved', AsyncMock(return_value=False)):
            receipt = await monthly_jobs.start(bg, 'month_2026-09', 'local_sync', True, True,
                AsyncMock(side_effect=RuntimeError('billing incomplete')))
        self.assertEqual('accepted', receipt['status'])
        self.assertIsNotNone(await db.get_active_ml_close_month_work('month_2026-09'))
        await bg()
        final = monthly_jobs.read(receipt['job_id'])
        self.assertEqual('failed', final['status'])
        self.assertIn('billing incomplete', final['error'])
        self.assertIsNone(await db.get_active_ml_close_month_work('month_2026-09'))

    async def test_approved_month_is_skipped_without_revoking_or_running(self):
        bg = BackgroundTasks(); runner = AsyncMock()
        with patch.object(monthly_jobs, '_approved', AsyncMock(return_value=True)):
            receipt = await monthly_jobs.start(bg, 'month_2026-08', 'cbt_ingest', True, True, runner)
        self.assertEqual('skipped_approved', receipt['status'])
        await bg(); runner.assert_not_awaited()
        self.assertIsNone(await db.get_active_ml_close_month_work('month_2026-08'))

    async def test_final_result_persists_and_parallel_writes_blocked(self):
        bg=BackgroundTasks()
        with patch.object(monthly_jobs,'_approved',AsyncMock(return_value=False)):
            receipt=await monthly_jobs.start(bg,'month_2026-09','local_sync',True,True,
                AsyncMock(return_value={'status':'synced','rows_verified':37}))
            with self.assertRaisesRegex(ValueError,'已有写入任务'):
                await monthly_jobs.start(BackgroundTasks(),'month_2026-09','cost',True,True,AsyncMock())
        await bg()
        self.assertEqual(37,monthly_jobs.read(receipt['job_id'])['result']['rows_verified'])

    async def test_restart_is_not_reported_as_success(self):
        receipt=await monthly_jobs.start(BackgroundTasks(),'month_2026-09','local_sync',False,False,AsyncMock())
        with patch.object(monthly_jobs,'BOOT_ID','restarted'):
            self.assertEqual('interrupted',monthly_jobs.read(receipt['job_id'])['status'])

    async def test_cleanup_error_still_saves_receipt_and_keeps_claim(self):
        bg=BackgroundTasks()
        receipt=await monthly_jobs.start(bg,'month_2026-09','cost',True,False,
            AsyncMock(return_value={'status':'ok'}))
        with patch.object(db,'complete_ml_close_action',AsyncMock(side_effect=RuntimeError('db unavailable'))):
            await bg()
        self.assertEqual('interrupted',monthly_jobs.read(receipt['job_id'])['status'])
        self.assertIsNotNone(await db.get_active_ml_close_month_work('month_2026-09'))

    async def test_cancelled_writer_retains_claim_beyond_old_timeout(self):
        bg=BackgroundTasks()
        receipt=await monthly_jobs.start(bg,'month_2026-09','cost',True,False,
            AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await bg()
        self.assertEqual('interrupted',monthly_jobs.read(receipt['job_id'])['status'])
        with patch.object(db.time,'time',return_value=time.time()+7200):
            self.assertIsNotNone(await db.get_active_ml_close_month_work('month_2026-09'))

    async def test_sync_caller_cannot_enter_during_background_write(self):
        await monthly_jobs.start(BackgroundTasks(),'month_2026-09','cost',True,False,AsyncMock())
        runner=AsyncMock()
        with self.assertRaisesRegex(ValueError,'已有写入任务'):
            await monthly_jobs.inline('month_2026-09','cost',runner)
        runner.assert_not_awaited()
