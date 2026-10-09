"""Final receipts for the existing monthly endpoints; no queue or new database.

The service runs one uvicorn worker. Existing monthly-work claims also keep
financial confirmation from observing an unfinished source/cost write.
"""
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from app import db

BOOT_ID = uuid.uuid4().hex


def _path(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("invalid job_id")
    return Path(db.DB_PATH).parent / "monthly-results" / f"{job_id}.json"


def _save(data):
    path = _path(data['job_id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)


def read(job_id):
    data = json.loads(_path(job_id).read_text(encoding='utf-8'))
    if data['status'] == 'running' and data['boot_id'] != BOOT_ID:
        data.update(status='interrupted', error='Service restarted; verify source rows before retrying.')
    return data


async def _approved(period):
    from app import ml_close
    status = await ml_close._get_status(period)
    fields = (status or {}).get('fields') or {}
    result = ml_close._last_result(fields)
    return bool(ml_close._text(fields.get('状态')) in
                ('运营已确认', '财务已确认暂结', '待最终核销', '财务已确认终稿')
                or result.get('operating_close_confirmed'))


async def start(background_tasks, period, operation, commit, automatic, runner):
    from app import ml_close
    job_id = uuid.uuid4().hex
    receipt = dict(job_id=job_id, period=period, operation=operation,
                   commit=commit, automatic=automatic, boot_id=BOOT_ID,
                   status='running', started_at=int(time.time()))
    if commit:
        async with ml_close._status_mutation_lock(period):
            if automatic and await _approved(period):
                receipt.update(status='skipped_approved', finished_at=int(time.time()))
                _save(receipt)
                return receipt
            if await db.get_active_ml_close_month_work(period):
                raise ValueError('本月已有写入任务运行；等待其最终结果后再试')
            await db.claim_ml_close_action(period, job_id, job_id)
            claim = await db.claim_ml_close_month_work(period, 'monthly_' + operation, job_id, job_id)
            if not claim.get('claimed'):
                await db.fail_ml_close_action(period, job_id, job_id, 'claim rejected')
                raise ValueError('本月任务未取得写入权；未执行')
    _save(receipt)
    background_tasks.add_task(_run, receipt, runner)
    return {**receipt, 'status': 'accepted', 'status_url': f'/report/monthly-result/{job_id}'}


async def inline(period, operation, runner):
    from fastapi import BackgroundTasks
    tasks = BackgroundTasks()
    receipt = await start(tasks, period, operation, True, False, runner)
    await tasks()
    final = read(receipt['job_id'])
    if final['status'] != 'succeeded':
        from fastapi import HTTPException
        raise HTTPException(final.get('http_status', 422), final.get('error', final['status']))
    return final['result']


async def _run(receipt, runner):
    period, job_id = receipt['period'], receipt['job_id']
    try:
        # A timeout must not unlock a thread-pool cost write that is still
        # running. The caller has its own bounded poll; the worker owns its
        # claim until actual completion or service restart.
        result = await runner()
        expected = {'local_sync': ('synced',) if receipt['commit'] else ('preview',), 'cbt_ingest': ('ok',),
                    'cost': ('ok',), 'review': ('ok',)}[receipt['operation']]
        if result.get('status') not in expected:
            raise RuntimeError(f"Monthly task returned {result.get('status')}")
        receipt.update(status='succeeded', result=result)
    except asyncio.CancelledError:
        receipt.update(status='interrupted', error='Worker cancelled; write may be incomplete. Reconcile before retry.')
        _save(receipt)
        # Keep the durable work claim: thread-pool work may still be alive.
        raise
    except Exception as exc:
        detail = getattr(exc, 'detail', str(exc))
        receipt.update(status='failed', http_status=getattr(exc, 'status_code', 422),
                       error=f'{type(exc).__name__}: {str(detail)[:2500]}')
    finally:
        receipt['finished_at'] = int(time.time())
        if receipt['commit'] and receipt['status'] != 'interrupted':
            ok = receipt['status'] == 'succeeded'
            try:
                # Release the monthly write claim last. A failed receipt cleanup
                # must not open a window for a second write.
                if ok:
                    await db.complete_ml_close_action(period, job_id, job_id)
                else:
                    await db.fail_ml_close_action(period, job_id, job_id, receipt.get('error',''))
                await db.finish_ml_close_month_work(period, job_id, 'completed' if ok else 'failed', receipt.get('error',''))
            except Exception as exc:
                receipt.update(status='interrupted', error=f'Result cleanup failed; reconcile before retry: {type(exc).__name__}')
        _save(receipt)
