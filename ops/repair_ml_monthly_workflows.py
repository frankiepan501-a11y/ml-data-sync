#!/usr/bin/env python3
"""Surgically repair the two Mercado Libre monthly n8n workflows.

The script always GETs the complete workflow, changes only the named nodes and
connections, then PUTs the complete document.  It never prints embedded header
values.  Default mode is dry-run; pass --commit for the production write.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request


MONTHLY_WORKFLOW_ID = "9ZvARULB0wIp19yp"
CBT_WORKFLOW_ID = "j5I4vcjwarGgols0"

MONTH_CODE = """const now = new Date(Date.now() + 8 * 3600 * 1000);
const d = new Date(now.getFullYear(), now.getMonth() - 1, 1);
const month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
return [{ json: { month, period: `month_${month}` } }];"""

ACTIVE_LOCAL_SELLERS = (2378517428, 3383185411)

PREPARE_CODE = """const now = new Date(Date.now() + 8*3600*1000); // cron runs on BJ day 2, after LATAM month close
const d = new Date(now.getFullYear(), now.getMonth() - 1, 1);
const month = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
const sellers = [2378517428, 3383185411]; // active local stores; CBT uses the official-export workflow
return sellers.map(s => ({json: {seller_id: s, month}}));
"""


def _node(workflow: dict, name: str) -> dict:
    matches = [node for node in workflow.get("nodes", []) if node.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one node named {name!r}; found {len(matches)}")
    return matches[0]


def _remove_never_error(options: dict) -> dict:
    cleaned = copy.deepcopy(options)

    def visit(value):
        if isinstance(value, dict):
            value.pop("neverError", None)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(cleaned)
    return cleaned


def _backfill_code(previous_code: str) -> str:
    token_line_match = re.search(r"(?m)^\s*const tok\s*=.*$", previous_code or "")
    if not token_line_match or "Bearer " not in token_line_match.group(0):
        raise RuntimeError("backfill node no longer contains the expected existing token assignment")
    token_line = token_line_match.group(0).strip()
    return f"""const now = new Date(Date.now() + 8*3600*1000);
const d = new Date(now.getFullYear(), now.getMonth() - 1, 1);
const month = `${{d.getFullYear()}}-${{String(d.getMonth()+1).padStart(2,'0')}}`;
const sellers = [2378517428, 3383185411];
// Allow 10 minutes of cross-service clock skew. Details fetched within that
// recent window are already fresh enough; all older scoped orders are refreshed.
const refreshAfter = Math.floor(Date.now() / 1000) - 600;
{token_line}
const results = [];
for (const seller_id of sellers) {{
  let completed = false;
  for (let attempt = 1; attempt <= 30; attempt++) {{
    let response;
    try {{
      response = await this.helpers.httpRequest({{
        method: 'POST',
        url: `https://ml-sync.zeabur.app/admin/backfill-orders?seller_id=${{seller_id}}&month=${{month}}&max_detail_fetch=100&refresh_after=${{refreshAfter}}`,
        headers: {{ Authorization: tok }},
        json: true,
        timeout: 120000,
      }});
    }} catch (error) {{
      results.push({{seller_id, attempt, transient_error: String(error?.message || error).slice(0, 200)}});
      if (attempt < 30) {{
        await new Promise(resolve => setTimeout(resolve, 2000));
        continue;
      }}
      throw error;
    }}
    if (!response || response.status !== 'backfilled') {{
      throw new Error(`ML month backfill failed seller=${{seller_id}} month=${{month}} response=${{JSON.stringify(response).slice(0,300)}}`);
    }}
    results.push({{seller_id, attempt, platform_total: response.platform_total, window_orders: response.window_orders, orders_with_detail: response.orders_with_detail, cached_month_unique: response.cached_month_unique, month_scope_replaced: response.month_scope_replaced, new_fetches: response.new_fetches, skipped_429: response.skipped_429, skipped_other: response.skipped_other, capped: response.capped}});
    if (Number.isInteger(response.platform_total) && response.month_scope_replaced === true && response.new_fetches === 0 && response.capped === false && (response.skipped_429 || 0) === 0 && (response.skipped_other || 0) === 0 && response.window_orders === response.platform_total && response.cached_month_unique === response.platform_total) {{
      completed = true;
      break;
    }}
  }}
  if (!completed) {{
    throw new Error(`ML month backfill did not converge seller=${{seller_id}} month=${{month}} after 30 passes`);
  }}
}}
return sellers.map(seller_id => ({{json: {{
  seller_id,
  month,
  backfill_passes: results.filter(item => item.seller_id === seller_id),
}}}}));
"""


def patch_monthly_workflow(workflow: dict) -> dict:
    if workflow.get("id") != MONTHLY_WORKFLOW_ID:
        raise RuntimeError(f"unexpected monthly workflow id {workflow.get('id')!r}")
    patched = copy.deepcopy(workflow)
    patched["name"] = "ML monthly aggregate · 每月 2 号 02:00 BJ · 2 active local sellers"
    schedule = _node(patched, "Monthly 02:00 BJ")
    intervals = schedule["parameters"]["rule"]["interval"]
    if len(intervals) != 1 or intervals[0].get("expression") not in {"0 2 1 * *", "0 2 2 * *"}:
        raise RuntimeError("monthly schedule no longer matches the reviewed day-1/day-2 baseline")
    intervals[0]["expression"] = "0 2 2 * *"

    _node(patched, "Prepare sellers + month")["parameters"]["jsCode"] = PREPARE_CODE
    backfill = _node(patched, "Backfill 本土店 (own-token)")
    backfill["parameters"]["jsCode"] = _backfill_code(backfill["parameters"].get("jsCode", ""))

    sync = _node(patched, "POST /report/sync-feishu-monthly")["parameters"]
    sync["url"] = "=https://ml-sync.zeabur.app/report/sync-feishu-monthly?seller_id={{ $json.seller_id }}&month={{ $json.month }}&commit=true"
    sync["options"] = _remove_never_error(sync.get("options") or {})
    sync["options"]["timeout"] = 300000

    cost = _node(patched, "POST /report/sync-meitong-cost")["parameters"]
    cost["options"] = _remove_never_error(cost.get("options") or {})
    return patched


def patch_cbt_workflow(workflow: dict) -> dict:
    if workflow.get("id") != CBT_WORKFLOW_ID:
        raise RuntimeError(f"unexpected CBT workflow id {workflow.get('id')!r}")
    patched = copy.deepcopy(workflow)
    schedule = _node(patched, "Schedule Trigger")
    ingest = _node(patched, "CBT ingest")
    allowed_urls = {
        "https://ml-sync.zeabur.app/report/cbt-ingest?commit=true",
        "=https://ml-sync.zeabur.app/report/cbt-ingest?month={{$json.month}}&commit=false",
    }
    if ingest["parameters"].get("url") not in allowed_urls:
        raise RuntimeError("CBT ingest URL no longer matches the reviewed baseline")
    ingest["parameters"]["url"] = "=https://ml-sync.zeabur.app/report/cbt-ingest?month={{$json.month}}&commit=false"

    build = {
        "id": "build-target-month",
        "name": "Build target month",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [380, 300],
        "parameters": {"jsCode": MONTH_CODE},
    }
    existing_build = [node for node in patched["nodes"] if node.get("name") == build["name"]]
    if len(existing_build) > 1:
        raise RuntimeError("CBT workflow contains duplicate Build target month nodes")
    if existing_build:
        existing_build[0]["parameters"] = build["parameters"]
    else:
        patched["nodes"].append(build)
    schedule_connections = patched["connections"].get(schedule["name"])
    allowed_schedule_connections = (
        {"main": [[{"node": ingest["name"], "type": "main", "index": 0}]]},
        {"main": [[{"node": build["name"], "type": "main", "index": 0}]]},
    )
    if schedule_connections not in allowed_schedule_connections:
        raise RuntimeError("CBT schedule connection no longer matches the reviewed baseline")
    removed_names = {"Build next card request", "Send next card"}
    patched["nodes"] = [node for node in patched["nodes"] if node.get("name") not in removed_names]
    for name in removed_names | {ingest["name"], build["name"]}:
        patched["connections"].pop(name, None)
    patched["connections"][schedule["name"]] = {
        "main": [[{"node": build["name"], "type": "main", "index": 0}]]
    }
    patched["connections"][build["name"]] = {
        "main": [[{"node": ingest["name"], "type": "main", "index": 0}]]
    }
    return patched


def _rest_base() -> str:
    base = (os.getenv("N8N_REST_BASE") or os.getenv("N8N_BASE_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("N8N_BASE_URL is required")
    return base if base.endswith("/api/v1") else base + "/api/v1"


def _request(method: str, path: str, body: dict | None = None) -> dict:
    api_key = os.getenv("N8N_API_KEY") or ""
    if not api_key:
        raise SystemExit("N8N_API_KEY is required")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _rest_base() + path,
        data=data,
        method=method,
        headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json"},
    )
    raw = ""
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(attempt * 2)
    return json.loads(raw) if raw else {}


def _safe_hash(workflow: dict) -> str:
    # n8n mutates versionId/updatedAt and other server-owned metadata on every PUT.
    # Only hash the fields this script owns and sends back.
    safe = copy.deepcopy(_put_body(workflow))
    # Secrets are included in the digest so an accidental credential rewrite is
    # detectable, but only the irreversible digest is ever printed.
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _put_body(workflow: dict) -> dict:
    return {
        "name": workflow["name"],
        "nodes": workflow["nodes"],
        "connections": workflow["connections"],
        "settings": workflow.get("settings") or {},
    }


def prepare_one(workflow_id: str, patcher, commit: bool) -> dict:
    before = _request("GET", f"/workflows/{workflow_id}")
    patched = patcher(before)
    summary = {
        "id": workflow_id,
        "active_before": bool(before.get("active")),
        "version_before": before.get("versionId"),
        "nodes_before": len(before.get("nodes") or []),
        "nodes_after": len(patched.get("nodes") or []),
        "safe_hash_before": _safe_hash(before),
        "safe_hash_after": _safe_hash(patched),
        "commit": commit,
    }
    return {"before": before, "patched": patched, "summary": summary}

def commit_one(prepared: dict) -> dict:
    before = prepared["before"]
    patched = prepared["patched"]
    summary = prepared["summary"]
    workflow_id = summary["id"]
    current = _request("GET", f"/workflows/{workflow_id}")
    if _safe_hash(current) == _safe_hash(patched) and bool(current.get("active")) == bool(before.get("active")):
        summary.update({
            "active_after": bool(current.get("active")),
            "version_after": current.get("versionId"),
            "nodes_verified": len(current.get("nodes") or []),
            "safe_hash_verified": _safe_hash(current),
            "verified": True,
            "no_op": True,
        })
        return summary
    if (
        current.get("versionId") != before.get("versionId")
        or _safe_hash(current) != _safe_hash(before)
        or bool(current.get("active")) != bool(before.get("active"))
    ):
        raise RuntimeError(f"workflow {workflow_id} changed after review; aborting PUT")
    written = _request("PUT", f"/workflows/{workflow_id}", _put_body(patched))
    if before.get("active") and not written.get("active"):
        written = _request("POST", f"/workflows/{workflow_id}/activate", {})
    after = _request("GET", f"/workflows/{workflow_id}")
    summary.update({
        "active_after": bool(after.get("active")),
        "version_after": after.get("versionId"),
        "nodes_verified": len(after.get("nodes") or []),
        "safe_hash_verified": _safe_hash(after),
        "verified": _safe_hash(after) == _safe_hash(patched) and bool(after.get("active")) == bool(before.get("active")),
        "no_op": False,
    })
    if not summary["verified"]:
        raise RuntimeError(f"workflow {workflow_id} read-back verification failed")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    # Preflight both workflows before the first production PUT. This prevents a
    # late baseline mismatch in workflow 2 from leaving workflow 1 half-deployed.
    prepared = [
        prepare_one(MONTHLY_WORKFLOW_ID, patch_monthly_workflow, args.commit),
        prepare_one(CBT_WORKFLOW_ID, patch_cbt_workflow, args.commit),
    ]
    results = [commit_one(item) for item in prepared] if args.commit else [item["summary"] for item in prepared]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
