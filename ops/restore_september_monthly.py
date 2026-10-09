"""Minimal node replacement on the existing monthly and cost workflows.

Run via a reviewed caller that GETs/backs up the complete live workflow,
then PUTs nodes, connections and settings and reads them back.
"""
import copy
import json

POLL_CODE = """
const base = 'https://ml-sync.zeabur.app';
const headers = {Authorization: AUTH};
const request = async (method, path) => await this.helpers.httpRequest({method,
  url: base + path, headers, json: true, timeout: 65000});
const finish = async (path) => {
  const accepted = await request('POST', path);
  if (accepted.status === 'skipped_approved') return accepted;
  if (accepted.status !== 'accepted' || !accepted.job_id) throw new Error('Monthly job was not accepted');
  for (let i = 0; i < 100; i++) {
    await new Promise(resolve => setTimeout(resolve, 15000));
    const done = await request('GET', '/report/monthly-result/' + accepted.job_id);
    if (done.status === 'succeeded') return done;
    if (done.status !== 'running') throw new Error(JSON.stringify({job_id: done.job_id, status: done.status, error: done.error}));
  }
  throw new Error('Monthly job final result timed out; do not run dependent cost steps');
};
"""


def _code_node(node, code):
    node['type'] = 'n8n-nodes-base.code'
    node['typeVersion'] = 2
    node['parameters'] = {'jsCode': code}
    node.pop('continueOnFail', None)
    node.pop('onError', None)


def _auth(node):
    return next(x['value'].lstrip('=') for x in node['parameters']['headerParameters']['parameters']
                if x['name'].lower() == 'authorization')


def patch_workflow(workflow):
    result = copy.deepcopy(workflow)
    nodes = {n['name']:n for n in result['nodes']}
    if workflow['id'] == '9ZvARULB0wIp19yp':
        for name, operation in [('POST /report/sync-feishu-monthly', 'local'), ('POST /report/sync-meitong-cost', 'cost')]:
            node = nodes[name]
            prefix = POLL_CODE.replace('AUTH', json.dumps(_auth(node)))
            if operation == 'local':
                code = """
const {seller_id, month} = $input.first().json;
if (![2378517428,3383185411].includes(Number(seller_id))) throw new Error('Unexpected seller');
const backup = `month_${month}_auto_${Date.now()}`;
const done = await finish(`/report/sync-feishu-monthly?seller_id=${seller_id}&month=${month}&commit=true&nowait=true&automatic=true&preserve_existing_as=${backup}`);
if (done.status === 'succeeded' && (done.result.status !== 'synced' || done.result.rows_written <= 0 || done.result.rows_verified !== done.result.rows_written)) throw new Error('Monthly source rows not verified');
return [{json: {...done, seller_id, month}}];
"""
            else:
                code = """
const period = $input.first().json.period;
if (!/^month_\d{4}-\d{2}$/.test(period)) throw new Error('Invalid period');
const done = await finish(`/report/sync-meitong-cost?period=${period}&commit=true&nowait=true&automatic=true`);
return [{json: done}];
"""
            _code_node(node, prefix + code)
    elif workflow['id'] == 'CWnmOuOmrde5bIkG':
        node = nodes['Recalc cost + audit']
        prefix = POLL_CODE.replace('AUTH', json.dumps(_auth(node)))
        code = """
const {period} = $input.first().json;
if (!/^month_\d{4}-\d{2}$/.test(period)) throw new Error('Invalid period');
const done = await finish(`/report/ml-close/recalc-cost?period=${period}&commit=true&nowait=true&automatic=true`);
if (done.status === 'skipped_approved') return [];
return [{json: done.result}];
"""
        _code_node(node, prefix + code)
    elif workflow['id'] == 'j5I4vcjwarGgols0':
        node = nodes['CBT ingest']
        prefix = POLL_CODE.replace('AUTH', json.dumps(_auth(node)))
        code = """
const {month} = $input.first().json;
if (!/^\d{4}-\d{2}$/.test(month)) throw new Error('Invalid month');
const backup = `month_${month}_auto_${Date.now()}`;
const source = await finish(`/report/cbt-ingest?month=${month}&commit=true&finalize=false&nowait=true&automatic=true&preserve_existing_as=${backup}`);
if (source.status === 'skipped_approved') return [{json: source}];
if (source.result.status !== 'ok' || source.result.rows_verified <= 0 || !(source.result.fx > 0)) throw new Error('CBT source or FX not verified');
const cost = await finish(`/report/sync-meitong-cost?period=month_${month}&commit=true&nowait=true&automatic=true`);
return [{json: {source, cost}}];
"""
        _code_node(node, prefix + code)
    else:
        raise ValueError('Workflow outside authorized scope')
    return result
