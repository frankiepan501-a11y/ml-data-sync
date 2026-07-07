#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update n8n Mercado Libre monthly-close workflows.

Env:
  N8N_BASE_URL or N8N_REST_BASE
  N8N_API_KEY
  ML_SYNC_SERVICE_AUTH_TOKEN
"""
from __future__ import annotations

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


def put_wf(wf: dict, nodes: list[dict], connections: dict) -> None:
    body = {
        "name": wf["name"],
        "nodes": nodes,
        "connections": connections,
        "settings": wf.get("settings") or {},
    }
    out = req("PUT", f"/workflows/{wf['id']}", body, timeout=120) or {}
    if wf.get("active") and not out.get("active"):
        try:
            req("POST", f"/workflows/{wf['id']}/activate", {}, timeout=60)
        except Exception:
            pass
    print(f"updated {wf['id']} {wf['name']}")


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
        http_node("cbt-ingest", "CBT ingest", 500, 300, "https://ml-sync.zeabur.app/report/cbt-ingest?commit=true", 300000),
        code_node(
            "build-next-card",
            "Build next card request",
            740,
            300,
            """const j = $input.first().json;
const audit = j.post_ingest?.ml_close_audit || {};
const period = audit.period || (j.month ? `month_${j.month}` : '');
const kind = audit.next_card || 'cost_gap';
return [{ json: { period, kind, url: `https://ml-sync.zeabur.app/report/ml-close/card?period=${encodeURIComponent(period)}&kind=${encodeURIComponent(kind)}&send=true` } }];""",
        ),
        http_node("send-next-card", "Send next card", 980, 300, "={{$json.url}}", 180000),
    ]
    connections = {
        sched["name"]: {"main": [[{"node": "CBT ingest", "type": "main", "index": 0}]]},
        "CBT ingest": {"main": [[{"node": "Build next card request", "type": "main", "index": 0}]]},
        "Build next card request": {"main": [[{"node": "Send next card", "type": "main", "index": 0}]]},
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


def update_gated(wid: str, target_url: str) -> None:
    wf = get_wf(wid)
    sched = trigger(wf)
    route_code = f"""const s = $input.first().json;
if (s.ready_for_finance) {{
  return [{{ json: {{ mode: 'run', url: '{target_url}' }} }}];
}}
return [{{ json: {{ mode: 'blocked', url: `https://ml-sync.zeabur.app/report/ml-close/card?period=${{encodeURIComponent(s.period)}}&send=true` }} }}];"""
    nodes = [
        sched,
        code_node("build-period", "Build period", 460, 300, PERIOD_CODE),
        http_node("ml-close-status", "Check ML close status", 700, 300, "=https://ml-sync.zeabur.app/report/ml-close/status?period={{$json.period}}", 90000),
        code_node("route-by-status", "Route by ML status", 940, 300, route_code),
        http_node("execute-route", "Execute gated route", 1180, 300, "={{$json.url}}", 180000),
    ]
    connections = {
        sched["name"]: {"main": [[{"node": "Build period", "type": "main", "index": 0}]]},
        "Build period": {"main": [[{"node": "Check ML close status", "type": "main", "index": 0}]]},
        "Check ML close status": {"main": [[{"node": "Route by ML status", "type": "main", "index": 0}]]},
        "Route by ML status": {"main": [[{"node": "Execute gated route", "type": "main", "index": 0}]]},
    }
    put_wf(wf, nodes, connections)


def main() -> None:
    update_instruction()
    update_cbt_ingest()
    update_cost_audit()
    update_gated("OzSSlkVa2b2y2aNS", "https://finance-report-audit.zeabur.app/aggregate")
    update_gated("aEzy1jZzG8lIEnss", "https://finance-report-audit.zeabur.app/report-monthly")
    for wid in ["ucq2vYbWVWiY98Fw", "j5I4vcjwarGgols0", "CWnmOuOmrde5bIkG", "OzSSlkVa2b2y2aNS", "aEzy1jZzG8lIEnss"]:
        wf = get_wf(wid)
        print(json.dumps({"id": wid, "name": wf["name"], "active": wf["active"], "nodes": len(wf["nodes"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
