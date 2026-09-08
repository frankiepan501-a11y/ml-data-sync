#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update n8n Mercado Libre monthly-close workflows.

Env:
  N8N_BASE_URL or N8N_REST_BASE
  N8N_API_KEY
  ML_SYNC_SERVICE_AUTH_TOKEN
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request


def _rest_base() -> str:
    base = (os.getenv("N8N_REST_BASE") or os.getenv("N8N_BASE_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("N8N_BASE_URL is required")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    return base


BASE = _rest_base()
N8N_KEY = os.getenv("N8N_API_KEY") or ""
ML_TOKEN = os.getenv("ML_SYNC_SERVICE_AUTH_TOKEN") or ""
if not N8N_KEY:
    raise SystemExit("N8N_API_KEY is required")
if not ML_TOKEN:
    raise SystemExit("ML_SYNC_SERVICE_AUTH_TOKEN is required")

HEADERS = {"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json"}
ML_AUTH = "Bearer " + ML_TOKEN


def req(method: str, path: str, data: dict | None = None, timeout: int = 90) -> dict | None:
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=body, method=method, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def get_wf(wid: str) -> dict:
    wf = req("GET", f"/workflows/{wid}")
    if not isinstance(wf, dict) or "nodes" not in wf:
        raise RuntimeError(f"Unexpected workflow response for {wid}: {str(wf)[:300]}")
    return wf


def _owned_body(wf: dict, nodes: list[dict] | None = None, connections: dict | None = None) -> dict:
    return {
        "name": wf["name"],
        "nodes": wf.get("nodes") if nodes is None else nodes,
        "connections": wf.get("connections") if connections is None else connections,
        "settings": wf.get("settings") or {},
    }


def _owned_hash(wf: dict, nodes: list[dict] | None = None, connections: dict | None = None) -> str:
    raw = json.dumps(
        _owned_body(wf, nodes, connections),
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def put_wf(wf: dict, nodes: list[dict], connections: dict) -> None:
    current = get_wf(wf["id"])
    if (
        current.get("versionId") != wf.get("versionId")
        or _owned_hash(current) != _owned_hash(wf)
        or bool(current.get("active")) != bool(wf.get("active"))
    ):
        raise RuntimeError(f"workflow {wf['id']} changed after GET; aborting PUT")
    body = _owned_body(wf, nodes, connections)
    out = req("PUT", f"/workflows/{wf['id']}", body, timeout=120) or {}
    if wf.get("active") and not out.get("active"):
        req("POST", f"/workflows/{wf['id']}/activate", {}, timeout=60)
    after = get_wf(wf["id"])
    if (
        _owned_hash(after) != _owned_hash(wf, nodes, connections)
        or bool(after.get("active")) != bool(wf.get("active"))
    ):
        raise RuntimeError(f"workflow {wf['id']} read-back verification failed")
    print(f"updated and verified {wf['id']} {wf['name']}")


def trigger(wf: dict) -> dict:
    for node in wf["nodes"]:
        if node.get("type", "").endswith("scheduleTrigger"):
            return node
    raise RuntimeError(f"no schedule trigger: {wf['id']} {wf['name']}")


def http_node(id_: str, name: str, x: int, y: int, url: str, timeout: int = 180000, auth: bool = True) -> dict:
    params: dict = {
        "method": "POST",
        "url": url,
        "sendHeaders": auth,
        "options": {"timeout": timeout},
    }
    if auth:
        params["headerParameters"] = {"parameters": [{"name": "Authorization", "value": ML_AUTH}]}
    return {
        "id": id_,
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [x, y],
        "parameters": params,
    }


def code_node(id_: str, name: str, x: int, y: int, code: str) -> dict:
    return {
        "id": id_,
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [x, y],
        "parameters": {"jsCode": code},
    }


PERIOD_CODE = """const now = new Date(Date.now() + 8 * 3600 * 1000);
const d = new Date(now.getFullYear(), now.getMonth() - 1, 1);
const month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
const period = `month_${month}`;
return [{ json: { month, period } }];"""


def update_instruction() -> None:
    wf = get_wf("ucq2vYbWVWiY98Fw")
    sched = trigger(wf)
    nodes = [
        sched,
        code_node("build-period", "Build period", 460, 300, PERIOD_CODE),
        http_node(
            "send-instruction-card",
            "Send instruction card",
            700,
            300,
            "=https://ml-sync.zeabur.app/report/ml-close/card?kind=instruction&period={{$json.period}}&send=true&receive_id=oc_cd007a8f1dbb4a78943625e5432a4cd7",
        ),
    ]
    connections = {
        sched["name"]: {"main": [[{"node": "Build period", "type": "main", "index": 0}]]},
        "Build period": {"main": [[{"node": "Send instruction card", "type": "main", "index": 0}]]},
    }
    put_wf(wf, nodes, connections)


def update_cbt_ingest() -> None:
    wf = get_wf("j5I4vcjwarGgols0")
    sched = trigger(wf)
    nodes = [
        sched,
        code_node(
            "build-target-month",
            "Build target month",
            380,
            300,
            """const now = new Date(Date.now() + 8 * 3600 * 1000);
const d = new Date(now.getFullYear(), now.getMonth() - 1, 1);
const month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
return [{ json: { month } }];""",
        ),
        # Recurring execution only validates that the three official files match
        # the target month. A controlled A/B check is required before commit.
        http_node("cbt-ingest", "CBT ingest", 560, 300, "=https://ml-sync.zeabur.app/report/cbt-ingest?month={{$json.month}}&commit=false", 300000),
    ]
    connections = {
        sched["name"]: {"main": [[{"node": "Build target month", "type": "main", "index": 0}]]},
        "Build target month": {"main": [[{"node": "CBT ingest", "type": "main", "index": 0}]]},
    }
    put_wf(wf, nodes, connections)


def update_cost_audit() -> None:
    wf = get_wf("CWnmOuOmrde5bIkG")
    sched = trigger(wf)
    nodes = [
        sched,
        code_node("build-period", "Build period", 460, 300, PERIOD_CODE),
        http_node("recalc-cost", "Recalc cost + audit", 700, 300, "=https://ml-sync.zeabur.app/report/ml-close/recalc-cost?period={{$json.period}}&commit=true", 300000),
        code_node(
            "build-cost-card",
            "Build cost card request",
            940,
            300,
            """const j = $input.first().json;
const audit = j.audit || j;
const period = audit.period || j.period;
const kind = audit.next_card || 'cost_gap';
return [{ json: { period, kind, url: `https://ml-sync.zeabur.app/report/ml-close/card?period=${encodeURIComponent(period)}&kind=${encodeURIComponent(kind)}&send=true` } }];""",
        ),
        http_node("send-cost-card", "Send cost card", 1180, 300, "={{$json.url}}", 180000),
    ]
    connections = {
        sched["name"]: {"main": [[{"node": "Build period", "type": "main", "index": 0}]]},
        "Build period": {"main": [[{"node": "Recalc cost + audit", "type": "main", "index": 0}]]},
        "Recalc cost + audit": {"main": [[{"node": "Build cost card request", "type": "main", "index": 0}]]},
        "Build cost card request": {"main": [[{"node": "Send cost card", "type": "main", "index": 0}]]},
    }
    put_wf(wf, nodes, connections)


def update_gated(wid: str) -> None:
    """Switch the gate field without rebuilding or retargeting the workflow.

    Production owns the schedule, target URL and auth expressions.  Replacing
    the whole five-node graph here can silently roll those live settings back,
    so this migration changes only the one status field it owns.
    """
    wf = get_wf(wid)
    nodes = json.loads(json.dumps(wf["nodes"], ensure_ascii=False))
    routes = [node for node in nodes if node.get("name") == "Route by ML status"]
    if len(routes) != 1:
        raise RuntimeError(f"workflow {wid} must contain exactly one Route by ML status node")
    code = str(routes[0].get("parameters", {}).get("jsCode") or "")
    if "s.ready_for_management" in code:
        print(f"already updated {wid} {wf['name']}")
        return
    if "s.ready_for_finance" not in code:
        raise RuntimeError(f"workflow {wid} does not contain the expected finance gate")
    routes[0]["parameters"]["jsCode"] = code.replace(
        "s.ready_for_finance", "s.ready_for_management", 1
    )
    put_wf(wf, nodes, wf.get("connections") or {})


def main() -> None:
    update_instruction()
    update_cbt_ingest()
    update_cost_audit()
    update_gated("OzSSlkVa2b2y2aNS")
    update_gated("aEzy1jZzG8lIEnss")
    for wid in ["ucq2vYbWVWiY98Fw", "j5I4vcjwarGgols0", "CWnmOuOmrde5bIkG", "OzSSlkVa2b2y2aNS", "aEzy1jZzG8lIEnss"]:
        wf = get_wf(wid)
        print(json.dumps({"id": wid, "name": wf["name"], "active": wf["active"], "nodes": len(wf["nodes"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
