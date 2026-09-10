# -*- coding: utf-8 -*-
"""Mercado Libre profit monthly close loop.

This module keeps the close-state logic in one place:
audit report rows, upsert the close status table, build interactive cards, and
handle button actions from Feishu event-hub.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import os
import time
import uuid
import weakref
from collections import defaultdict
from typing import Any

import anyio
import httpx

from app import db, meitong_cost
from app.unified_report import revision_cost_rounding_delta

FEISHU = "https://open.feishu.cn/open-apis"
APP_TOKEN = os.getenv("FEISHU_BASE_APP_TOKEN", "WM3LbBr76aRqMys2of8c1dGInEb")
REPORT_TABLE_ID = os.getenv("FEISHU_BASE_TABLE_ID", "tbl09sRPkX35PDfU")
BASE_URL = f"https://u1wpma3xuhr.feishu.cn/base/{APP_TOKEN}"
STATUS_TABLE_NAME = os.getenv("ML_CLOSE_STATUS_TABLE_NAME", "美客多毛利月结状态台")
STATUS_TABLE_ID_ENV = os.getenv("ML_CLOSE_STATUS_TABLE_ID", "")

ML_GROUP_ID = os.getenv("ML_CLOSE_GROUP_ID", "oc_cd007a8f1dbb4a78943625e5432a4cd7")
FINANCE_GROUP_ID = os.getenv("ML_CLOSE_FINANCE_GROUP_ID", "oc_6b2da626d80eb6284bbe9dcf895030b9")
CARD_APP_ID = os.getenv("FEISHU_CARD_APP_ID", "cli_a9457898bd78dccc")
CARD_APP_SECRET = os.getenv("FEISHU_CARD_APP_SECRET", "")

_STATUS_LOCKS_BY_LOOP: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()
_EMERGENCY_AD_FAILURES: dict[str, dict[str, int]] = {}
_ACTION_EPOCHS: dict[str, int] = {}
_ACTION_KEY_EPOCHS: dict[str, dict[str, int]] = {}

STATUSES = [
    "待数据同步",
    "待CBT上传",
    "待CBT解析",
    "待成本核算",
    "成本缺失待补",
    "待运营确认",
    "运营已确认",
    "财务已确认暂结",
    "待最终核销",
    "财务已确认终稿",
    "退回重算",
    "异常",
]

STATUS_FIELDS: list[dict[str, Any]] = [
    {"field_name": "月份", "type": 1},
    {"field_name": "状态", "type": 3, "property": {"options": [{"name": s} for s in STATUSES]}},
    {"field_name": "报表行数", "type": 2},
    {"field_name": "店铺覆盖数", "type": 2},
    {"field_name": "CBT解析状态", "type": 1},
    {"field_name": "成本缺口数", "type": 2},
    {"field_name": "采购缺口数", "type": 2},
    {"field_name": "头程/海外仓缺口数", "type": 2},
    {"field_name": "最近重算时间", "type": 5},
    {"field_name": "运营确认人", "type": 1},
    {"field_name": "运营确认时间", "type": 5},
    {"field_name": "财务确认人", "type": 1},
    {"field_name": "财务确认时间", "type": 5},
    {"field_name": "经营暂结确认人", "type": 1},
    {"field_name": "经营暂结确认时间", "type": 5},
    {"field_name": "经营暂结报表链接", "type": 1},
    {"field_name": "最终核销状态", "type": 1},
    {"field_name": "最终核销截止日", "type": 5},
    {"field_name": "报表链接", "type": 1},
    {"field_name": "缺口视图链接", "type": 1},
    {"field_name": "最后卡片 message_id", "type": 1},
    {"field_name": "最后按钮动作Key", "type": 1},
    {"field_name": "最后按钮动作时间", "type": 5},
    {"field_name": "重算次数", "type": 2},
    {"field_name": "上次全额毛利", "type": 2},
    {"field_name": "全额毛利差异", "type": 2},
    {"field_name": "最后错误", "type": 1},
    {"field_name": "最后结果JSON", "type": 1},
]


def normalize_period(month: str | None = None, period: str | None = None) -> tuple[str, str]:
    if period:
        p = period.strip()
        if p.startswith("month_"):
            return p, p.removeprefix("month_")
        return f"month_{p}", p
    if month:
        m = month.strip().removeprefix("month_")
        return f"month_{m}", m
    last = _dt.date.today().replace(day=1) - _dt.timedelta(days=1)
    m = last.strftime("%Y-%m")
    return f"month_{m}", m


async def _tenant_token(app_id: str | None = None, secret: str | None = None) -> str:
    app_id = app_id or os.getenv("FEISHU_APP_ID", "cli_a9f6ae86fce8dbd8")
    secret = secret if secret is not None else os.getenv("FEISHU_APP_SECRET", "")
    if not secret:
        raise RuntimeError(f"Feishu secret missing for app {app_id}")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{FEISHU}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": secret},
        )
    j = r.json()
    tok = j.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"tenant token failed: {j}")
    return tok


async def _fs_json(
    method: str,
    url: str,
    tok: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=utf-8"}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, url, headers=headers, json=payload)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Feishu non-json response {r.status_code}: {r.text[:500]}")
    if r.status_code >= 400 or j.get("code") not in (0, None):
        raise RuntimeError(f"Feishu API failed {method} {url}: {j}")
    return j


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "".join(_text(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or v.get("value") or "")
    return str(v)


def _num(v: Any) -> float:
    try:
        if isinstance(v, list) and v:
            v = v[0]
        if isinstance(v, dict):
            v = v.get("text") or v.get("value")
        if v in ("", None):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _blank_cost(v: Any) -> bool:
    if v in (None, ""):
        return True
    if isinstance(v, list) and not v:
        return True
    return abs(_num(v)) < 0.0001


def _money(v: float) -> str:
    return f"¥{v:,.2f}"


def _signed_money(v: float) -> str:
    sign = "+" if v > 0.0001 else ""
    return f"{sign}{_money(v)}"


def _fmt_ms(v: Any) -> str:
    ms = int(_num(v))
    if ms <= 0:
        return "-"
    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _last_result(fields: dict[str, Any]) -> dict[str, Any]:
    raw = _text(fields.get("最后结果JSON"))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _report_content_hash(rows: list[dict[str, Any]]) -> str:
    """Bind an A/B approval to the exact Base record version it reviewed."""
    normalized = [
        {
            "record_id": _text(row.get("record_id")),
            "fields": row.get("fields") or {},
        }
        for row in rows
    ]
    normalized.sort(key=lambda row: row["record_id"])
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest() if normalized else ""


def _failed_ad_shops(fields: dict[str, Any]) -> list[str]:
    result = _last_result(fields)
    shops = result.get("failed_ad_shops") or []
    if not isinstance(shops, list):
        return []
    return sorted({_text(shop).strip() for shop in shops if _text(shop).strip()})


def _ad_failure_message(shops: list[str]) -> str:
    names = "、".join(shops) if shops else "未识别店铺"
    return f"广告费抓取失败：{names}。本次月结已拦截，禁止把抓取失败自动记为 0。"


def _status_mutation_lock(period: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _STATUS_LOCKS_BY_LOOP.setdefault(loop, {})
    return locks.setdefault(period, asyncio.Lock())


def _with_ad_failure(base_error: str, failed_shops: list[str]) -> str:
    if not failed_shops:
        return base_error
    ad_failure = _ad_failure_message(failed_shops)
    return f"{ad_failure}；{base_error}" if base_error else ad_failure


def _close_state(
    has_rows: bool,
    has_cost_gaps: bool,
    prior_state: str,
    last_error: str,
    ab_verified: bool = False,
    operating_close_confirmed: bool = False,
) -> tuple[str, str]:
    if last_error:
        return "异常", "error"
    if not has_rows:
        return "待数据同步", "instruction"
    if has_cost_gaps:
        return "成本缺失待补", "cost_gap"
    if prior_state == "财务已确认终稿":
        return prior_state, "none"
    if operating_close_confirmed or prior_state == "财务已确认暂结":
        if ab_verified:
            return "待最终核销", "ops_final"
        return "财务已确认暂结", "none"
    if prior_state == "运营已确认":
        return prior_state, "finance_final" if ab_verified else "finance_operating"
    return "待运营确认", "ops_final" if ab_verified else "ops_operating"


def _final_reconciliation_due(month: str) -> tuple[str, int]:
    year, month_number = (int(part) for part in month.split("-"))
    if month_number == 12:
        due = _dt.date(year + 1, 1, 18)
    else:
        due = _dt.date(year, month_number + 1, 18)
    due_ms = int(_dt.datetime.combine(due, _dt.time.min, tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp() * 1000)
    return due.isoformat(), due_ms


def _resolve_ab_verified(
    prior_fields: dict[str, Any],
    requested: bool | None,
    current_report_hash: str | None = None,
) -> bool:
    """Resolve A/B only when approval is bound to the current report version."""
    if requested is not None:
        return bool(requested and current_report_hash)
    result = _last_result(prior_fields)
    approved_hash = _text(result.get("ab_report_hash"))
    candidate_hash = current_report_hash if current_report_hash is not None else _text(result.get("report_hash"))
    return bool(result.get("ab_verified") and approved_hash and candidate_hash and approved_hash == candidate_hash)


async def invalidate_ab_verification(period: str, reason: str) -> dict[str, Any]:
    """Revoke A/B before any production writer can mutate the monthly report."""
    async with _status_mutation_lock(period):
        tok = await _tenant_token()
        status = await _get_status(period, tok)
        fields = status.get("fields", {}) if status else {}
        result = _last_result(fields)
        result.update(
            {
                "period": period,
                "ab_verified": False,
                "ab_report_hash": "",
                "ab_invalidated_reason": reason,
                "ab_invalidated_at": int(time.time() * 1000),
            }
        )
        return await _upsert_status(
            period,
            {
                "状态": "退回重算",
                "最后结果JSON": json.dumps(result, ensure_ascii=False),
            },
            tok,
        )


async def _open_ad_failures(period: str, status_fields: dict[str, Any] | None = None) -> list[str]:
    shops = set(_failed_ad_shops(status_fields or {}))
    shops.update(_EMERGENCY_AD_FAILURES.get(period, {}))
    for loader in (db.list_ad_sync_failures, db.list_ad_sync_failure_fallbacks):
        for row in await loader(period):
            shop = _text(row.get("shop")).strip()
            if shop:
                shops.add(shop)
    return sorted(shops)


def _store_details_section(summary: dict[str, Any]) -> dict[str, Any] | None:
    details = summary.get("store_details") or []
    if not details:
        stores = summary.get("stores") or []
        if not stores:
            return None
        text = "**覆盖店铺**\n" + "\n".join(f"- {s}" for s in stores)
        return {"tag": "div", "text": _md(text)}

    lines = ["**覆盖店铺明细**"]
    for row in details:
        store = row.get("store") or "未识别店铺"
        rows = int(row.get("rows") or 0)
        orders = int(row.get("orders") or 0)
        revenue = _money(float(row.get("revenue_rmb") or 0))
        ad_fee = _money(float(row.get("ad_fee_rmb") or 0))
        lines.append(f"- **{store}**：{rows} 行 / {orders} 单 / 营收 {revenue} / 广告费 {ad_fee}")
    return {"tag": "div", "text": _md("\n".join(lines))}


def _record_url(rid: str) -> str:
    return f"{BASE_URL}?table={REPORT_TABLE_ID}&record={rid}"


def _report_url() -> str:
    return f"{BASE_URL}?table={REPORT_TABLE_ID}"


def _status_url() -> str:
    return f"{BASE_URL}?table={STATUS_TABLE_ID_ENV}" if STATUS_TABLE_ID_ENV else BASE_URL


def _top_link_actions(report_url: str, gap_url: str | None = None) -> dict[str, Any]:
    actions = [_button("打开飞书毛利报表", url=report_url)]
    if gap_url and gap_url != report_url:
        actions.append(_button("打开缺口视图", url=gap_url))
    actions.append(_button("打开月结状态台", url=_status_url()))
    return {"tag": "action", "actions": actions}


async def _list_records(tok: str, table_id: str, field_names: list[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page_token = ""
    params = "page_size=500"
    if field_names:
        params += "".join(f"&field_names={name}" for name in field_names)
    while True:
        url = f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records?{params}"
        if page_token:
            url += f"&page_token={page_token}"
        d = (await _fs_json("GET", url, tok)).get("data", {})
        out.extend(d.get("items") or [])
        page_token = d.get("page_token") or ""
        if not d.get("has_more"):
            break
    return out


async def _status_table(tok: str) -> str:
    if STATUS_TABLE_ID_ENV:
        return STATUS_TABLE_ID_ENV
    page_token = ""
    while True:
        url = f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        d = (await _fs_json("GET", url, tok)).get("data", {})
        for table in d.get("items", []):
            if table.get("name") == STATUS_TABLE_NAME:
                return table["table_id"]
        page_token = d.get("page_token") or ""
        if not d.get("has_more"):
            break
    created = await _fs_json(
        "POST",
        f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables",
        tok,
        {"table": {"name": STATUS_TABLE_NAME}},
    )
    return created.get("data", {}).get("table_id") or created.get("data", {}).get("table", {}).get("table_id")


async def ensure_status_table(tok: str | None = None) -> dict[str, Any]:
    tok = tok or await _tenant_token()
    table_id = await _status_table(tok)
    fields_resp = await _fs_json(
        "GET",
        f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields?page_size=200",
        tok,
    )
    fields = fields_resp.get("data", {}).get("items", [])
    existing = {f.get("field_name"): f for f in fields}

    # New Feishu tables start with one default text field. Rename it to 周期 if needed.
    if "周期" not in existing and fields:
        first = fields[0]
        await _fs_json(
            "PUT",
            f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields/{first['field_id']}",
            tok,
            {"field_name": "周期", "type": first.get("type", 1)},
        )

    fields_resp = await _fs_json(
        "GET",
        f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields?page_size=200",
        tok,
    )
    existing_names = {f.get("field_name") for f in fields_resp.get("data", {}).get("items", [])}
    for spec in STATUS_FIELDS:
        if spec["field_name"] in existing_names:
            continue
        await _fs_json(
            "POST",
            f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields",
            tok,
            spec,
        )
    return {"table_id": table_id, "url": f"{BASE_URL}?table={table_id}"}


async def _upsert_status(period: str, fields: dict[str, Any], tok: str | None = None) -> dict[str, Any]:
    tok = tok or await _tenant_token()
    table = await ensure_status_table(tok)
    table_id = table["table_id"]
    rows = await _list_records(tok, table_id)
    rid = None
    for r in rows:
        if _text(r.get("fields", {}).get("周期")) == period:
            rid = r["record_id"]
            break
    fields = {"周期": period, **fields}
    if rid:
        await _fs_json(
            "PUT",
            f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/{rid}",
            tok,
            {"fields": fields},
        )
        return {"record_id": rid, "table_id": table_id, "updated": True, "url": f"{BASE_URL}?table={table_id}&record={rid}"}
    created = await _fs_json(
        "POST",
        f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records",
        tok,
        {"fields": fields},
    )
    rid = created.get("data", {}).get("record", {}).get("record_id") or created.get("data", {}).get("record_id")
    return {"record_id": rid, "table_id": table_id, "created": True, "url": f"{BASE_URL}?table={table_id}&record={rid}"}


async def _get_status(period: str, tok: str | None = None) -> dict[str, Any] | None:
    tok = tok or await _tenant_token()
    table = await ensure_status_table(tok)
    for r in await _list_records(tok, table["table_id"]):
        if _text(r.get("fields", {}).get("周期")) == period:
            return {"record_id": r["record_id"], "fields": r.get("fields", {}), "table_id": table["table_id"]}
    return None


async def _commit_audit_snapshot(
    result: dict[str, Any],
    tok: str,
    base_error: str,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    """Commit an already calculated audit while the caller owns status order."""
    period = _text(result.get("period"))
    month = _text(result.get("month"))
    latest = await _get_status(period, tok)
    latest_fields = latest.get("fields", {}) if latest else {}
    latest_state = _text(latest_fields.get("状态"))
    latest_result = _last_result(latest_fields)
    latest_marker_error = ""
    try:
        latest_failed_ad_shops = await _open_ad_failures(period, latest_fields)
    except Exception as exc:
        latest_failed_ad_shops = _failed_ad_shops(latest_fields)
        latest_marker_error = f"广告失败状态读取失败：{type(exc).__name__}"
    latest_base_error = (
        f"{base_error}；{latest_marker_error}"
        if base_error and latest_marker_error
        else (base_error or latest_marker_error)
    )
    last_error = _with_ad_failure(latest_base_error, latest_failed_ad_shops)
    state, next_card = _close_state(
        bool(result.get("report_rows")),
        bool(result.get("gap_row_count")),
        latest_state,
        last_error,
        bool(result.get("ab_verified")),
        bool(latest_result.get("operating_close_confirmed")),
    )
    result.update(
        {
            "status": "ok" if not last_error else "error",
            "state": state,
            "next_card": next_card,
            "last_error": last_error,
            "failed_ad_shops": latest_failed_ad_shops,
        }
    )
    previous_gross = round(float(_num(latest_result.get("gross_profit_rmb"))), 2)
    gross_profit = round(float(_num(result.get("gross_profit_rmb"))), 2)
    gross_delta = round(gross_profit - previous_gross, 2) if previous_gross else 0.0
    status_result = {
        "failure_type": "advertising_fetch" if latest_failed_ad_shops else "",
        "failed_ad_shops": latest_failed_ad_shops,
        "status": result["status"],
        "period": period,
        "month": month,
        "state": state,
        "next_card": next_card,
        "report_rows": int(_num(result.get("report_rows"))),
        "store_count": int(_num(result.get("store_count"))),
        "order_count": int(_num(result.get("order_count"))),
        "unit_count": _num(result.get("unit_count")),
        "revenue_rmb": _num(result.get("revenue_rmb")),
        "gross_profit_rmb": gross_profit,
        "ad_total_rmb": _num(result.get("ad_total_rmb")),
        "purchase_gap_count": int(_num(result.get("purchase_gap_count"))),
        "freight_gap_count": int(_num(result.get("freight_gap_count"))),
        "gap_row_count": int(_num(result.get("gap_row_count"))),
        "head_total_rmb": _num(result.get("head_total_rmb")),
        "ovs_total_rmb": _num(result.get("ovs_total_rmb")),
        "cbt_state": _text(result.get("cbt_state")),
        "last_error": last_error[:500],
        "ab_verified": bool(result.get("ab_verified")),
        "report_hash": _text(result.get("report_hash")),
        "ab_report_hash": (
            _text(result.get("report_hash")) if result.get("ab_verified") else ""
        ),
    }
    for key in (
        "review_source_hash",
        "review_report_url",
        "operating_close_confirmed",
        "operating_report_hash",
        "operating_content_hash",
        "operating_report_url",
        "operating_report_sheet_url",
        "operating_closed_at",
        "operating_closed_by",
        "final_reconciliation_due_on",
        "final_reconciliation_status",
        "provisional_items",
    ):
        if key in latest_result:
            status_result[key] = latest_result[key]
    status_result["final_reconciliation_due_on"] = (
        _text(status_result.get("final_reconciliation_due_on"))
        or _text(result.get("final_reconciliation_due_on"))
        or _final_reconciliation_due(month)[0]
    )
    status_result["final_reconciliation_status"] = (
        _text(result.get("final_reconciliation_status"))
        or _text(status_result.get("final_reconciliation_status"))
        or "待官方账单核销"
    )
    fields = {
        "月份": month,
        "状态": state,
        "报表行数": status_result["report_rows"],
        "店铺覆盖数": status_result["store_count"],
        "CBT解析状态": status_result["cbt_state"],
        "成本缺口数": status_result["gap_row_count"],
        "采购缺口数": status_result["purchase_gap_count"],
        "头程/海外仓缺口数": status_result["freight_gap_count"],
        "最近重算时间": int(time.time() * 1000),
        "报表链接": _report_url(),
        "缺口视图链接": _report_url(),
        "上次全额毛利": previous_gross,
        "全额毛利差异": gross_delta,
        "最后错误": last_error[:1800],
        "最后结果JSON": json.dumps(status_result, ensure_ascii=False),
        "重算次数": int(_num(latest_fields.get("重算次数"))) + 1,
        "最终核销状态": status_result.get("final_reconciliation_status") or "待官方账单核销",
        "最终核销截止日": _final_reconciliation_due(month)[1],
    }
    if extra_fields:
        fields.update(extra_fields)
    result["status_record"] = await _upsert_status(period, fields, tok)


async def audit(
    month: str | None = None,
    period: str | None = None,
    commit: bool = False,
    run_cost_preview: bool = True,
    cost_summary: dict[str, Any] | None = None,
    ab_verified: bool | None = None,
) -> dict[str, Any]:
    period, month = normalize_period(month, period)
    tok = await _tenant_token()
    prior = await _get_status(period, tok)
    prior_fields = prior.get("fields", {}) if prior else {}
    prior_state = _text(prior_fields.get("状态"))
    prior_result = _last_result(prior_fields)
    marker_error = ""
    try:
        prior_failed_ad_shops = await _open_ad_failures(period, prior_fields)
    except Exception as e:
        prior_failed_ad_shops = _failed_ad_shops(prior_fields)
        marker_error = f"广告失败状态读取失败：{type(e).__name__}"
    records = await _list_records(tok, REPORT_TABLE_ID)
    rows = [r for r in records if _text(r.get("fields", {}).get("周期")) == period]
    report_hash = _report_content_hash(rows)
    effective_ab_verified = _resolve_ab_verified(prior_fields, ab_verified, report_hash)

    cost_error = ""
    if cost_summary is None and run_cost_preview:
        try:
            cost_summary = await anyio.to_thread.run_sync(meitong_cost.run, period, 12, False)
        except Exception as e:
            cost_error = f"{type(e).__name__}: {e}"
            cost_summary = {"status": "error", "msg": cost_error}

    stores: set[str] = set()
    revenue = 0.0
    profit = 0.0
    head_total = 0.0
    ovs_total = 0.0
    order_count = 0
    unit_count = 0.0
    ad_total_rmb = 0.0
    commission_max_abs_delta = 0.0
    profit_max_abs_delta = 0.0
    store_details: dict[str, dict[str, Any]] = {}
    purchase_gaps: list[dict[str, Any]] = []
    freight_gaps: list[dict[str, Any]] = []
    gap_map: dict[str, dict[str, Any]] = {}

    for r in rows:
        f = r.get("fields", {})
        store = _text(f.get("店铺")) or "未识别店铺"
        sku = _text(f.get("SKU")) or "(空SKU)"
        rev = _num(f.get("营收(RMB)"))
        units = _num(f.get("件数"))
        orders = int(_num(f.get("订单数")))
        ad_fee = _num(f.get("广告费(RMB)"))
        cg = _num(f.get("采购成本(RMB)"))
        head = _num(f.get("头程成本(RMB)"))
        ovs = _num(f.get("海外仓成本(RMB)"))
        stores.add(store)
        sd = store_details.setdefault(store, {"store": store, "rows": 0, "orders": 0, "units": 0.0, "revenue_rmb": 0.0, "ad_fee_rmb": 0.0})
        sd["rows"] += 1
        sd["orders"] += orders
        sd["units"] += units
        sd["revenue_rmb"] += rev
        sd["ad_fee_rmb"] += ad_fee
        revenue += rev
        profit += _num(f.get("全额毛利(RMB)"))
        head_total += head
        ovs_total += ovs
        order_count += orders
        unit_count += units
        ad_total_rmb += ad_fee
        commission_max_abs_delta = max(
            commission_max_abs_delta,
            abs(
                _num(f.get("ML佣金(RMB)"))
                - _num(f.get("ML佣金(原币)")) * _num(f.get("我的汇率"))
            ),
        )
        calculated_profit = (
            rev
            + _num(f.get("调整(RMB)"))
            - cg
            - _num(f.get("ML佣金(RMB)"))
            - ad_fee
            - _num(f.get("VAT估算(RMB)"))
            - _num(f.get("物流费(RMB)"))
            - _num(f.get("退款金额(RMB)"))
            - _num(f.get("Full仓储费(RMB)"))
            - head
            - ovs
        )
        calculated_profit += revision_cost_rounding_delta(f)
        profit_max_abs_delta = max(
            profit_max_abs_delta,
            abs(_num(f.get("全额毛利(RMB)")) - calculated_profit),
        )
        active_row = rev > 0.0001 or units > 0.0001 or orders > 0
        if active_row and units > 0 and (cg <= 0.0001 or _blank_cost(f.get("采购成本(RMB)"))):
            purchase_gaps.append({"record_id": r["record_id"], "store": store, "sku": sku, "orders": orders, "units": units, "revenue": rev})
            gap_map.setdefault(r["record_id"], {"record_id": r["record_id"], "store": store, "sku": sku, "orders": orders, "units": units, "revenue": rev, "gap_types": []})["gap_types"].append("采购成本")
        if active_row and units > 0 and _blank_cost(f.get("头程成本(RMB)")) and _blank_cost(f.get("海外仓成本(RMB)")):
            freight_gaps.append({"record_id": r["record_id"], "store": store, "sku": sku, "orders": orders, "units": units, "revenue": rev})
            gap_map.setdefault(r["record_id"], {"record_id": r["record_id"], "store": store, "sku": sku, "orders": orders, "units": units, "revenue": rev, "gap_types": []})["gap_types"].append("头程/海外仓")

    gap_rows = list(gap_map.values())
    cbt_rows = [r for r in rows if "CBT" in _text(r.get("fields", {}).get("店铺"))]
    cbt_state = "已解析" if cbt_rows else "未发现CBT行"

    formula_errors: list[str] = []
    if commission_max_abs_delta > 0.05:
        formula_errors.append(
            f"佣金换算最大差额 {commission_max_abs_delta:.4f} RMB，超过 0.05 RMB"
        )
    if profit_max_abs_delta > 0.02:
        formula_errors.append(
            f"毛利公式最大差额 {profit_max_abs_delta:.4f} RMB，超过 0.02 RMB"
        )
    formula_error = "；".join(formula_errors)
    base_error = "；".join(
        part for part in (marker_error, cost_error, formula_error) if part
    )
    if cost_summary and cost_summary.get("status") == "error":
        cost_failure = _text(cost_summary.get("msg")) or json.dumps(cost_summary, ensure_ascii=False)[:500]
        base_error = f"{base_error}；{cost_failure}" if base_error else cost_failure
    last_error = _with_ad_failure(base_error, prior_failed_ad_shops)
    state, next_card = _close_state(
        bool(rows),
        bool(purchase_gaps or freight_gaps),
        prior_state,
        last_error,
        effective_ab_verified,
        bool(prior_result.get("operating_close_confirmed")),
    )

    result = {
        "status": "ok" if not last_error else "error",
        "period": period,
        "month": month,
        "commit": commit,
        "state": state,
        "next_card": next_card,
        "report_rows": len(rows),
        "store_count": len(stores),
        "stores": sorted(stores),
        "order_count": order_count,
        "unit_count": unit_count,
        "revenue_rmb": round(revenue, 2),
        "gross_profit_rmb": round(profit, 2),
        "ad_total_rmb": round(ad_total_rmb, 2),
        "commission_max_abs_delta": round(commission_max_abs_delta, 6),
        "commission_check": "通过" if commission_max_abs_delta <= 0.05 else "需复核",
        "profit_max_abs_delta": round(profit_max_abs_delta, 6),
        "profit_check": "通过" if profit_max_abs_delta <= 0.02 else "需复核",
        "store_details": [
            {
                "store": v["store"],
                "rows": int(v["rows"]),
                "orders": int(v["orders"]),
                "units": round(float(v["units"]), 2),
                "revenue_rmb": round(float(v["revenue_rmb"]), 2),
                "ad_fee_rmb": round(float(v["ad_fee_rmb"]), 2),
            }
            for v in sorted(store_details.values(), key=lambda x: x["store"])
        ],
        "purchase_gap_count": len(purchase_gaps),
        "freight_gap_count": len(freight_gaps),
        "gap_row_count": len(gap_rows),
        "head_total_rmb": round(head_total, 2),
        "ovs_total_rmb": round(ovs_total, 2),
        "cbt_state": cbt_state,
        "report_url": _report_url(),
        "gap_view_url": _report_url(),
        "gap_rows": gap_rows,
        "cost_summary": cost_summary or {},
        "_base_error": base_error,
        "last_error": last_error,
        "failed_ad_shops": prior_failed_ad_shops,
        "ab_verified": effective_ab_verified,
        "report_hash": report_hash,
        "operating_ready": bool(rows) and not bool(purchase_gaps or freight_gaps) and not bool(last_error),
        "operating_close_confirmed": bool(prior_result.get("operating_close_confirmed")),
        "operating_report_hash": _text(prior_result.get("operating_report_hash")),
        "operating_report_url": _text(prior_result.get("operating_report_url")),
        "final_reconciliation_due_on": (
            _text(prior_result.get("final_reconciliation_due_on"))
            or _final_reconciliation_due(month)[0]
        ),
        "final_reconciliation_status": (
            "已完成" if effective_ab_verified and prior_state == "财务已确认终稿" else "待官方账单核销"
        ),
    }

    if commit:
        async with _status_mutation_lock(period):
            await _commit_audit_snapshot(result, tok, base_error)
    return result


def _md(content: str) -> dict[str, str]:
    return {"tag": "lark_md", "content": content}


def _plain(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _field(label: str, value: str) -> dict[str, Any]:
    return {"is_short": True, "text": _md(f"**{label}**\n{value}")}


def _button(label: str, action: str | None = None, period: str | None = None, url: str | None = None,
            btn_type: str = "default", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    b: dict[str, Any] = {"tag": "button", "text": _plain(label), "type": btn_type}
    if url:
        b["url"] = url
    if action:
        value = {"action": action, "period": period}
        if extra:
            value.update(extra)
        b["value"] = value
    return b


def _title_for(kind: str, month: str) -> tuple[str, str]:
    if kind == "instruction":
        return "yellow", f"🟡 [FIN·P2] 美客多毛利本月操作指引 · {month}"
    if kind == "cost_gap":
        return "orange", f"🟠 [FIN·P1] 美客多成本缺口待处理 · {month}"
    if kind == "ops_operating":
        return "green", f"🟠 [FIN·P1] 美客多经营暂结待运营确认 · {month}"
    if kind == "ops_final":
        return "green", f"🟡 [FIN·P2] 美客多最终核销待运营确认 · {month}"
    if kind == "finance_operating":
        return "blue", f"🟠 [FIN·P1] 美客多经营暂结待财务放行 · {month}"
    if kind == "finance_final":
        return "blue", f"🟡 [FIN·P2] 美客多毛利待财务确认 · {month}"
    if kind == "processed":
        return "grey", f"✅ [FIN·P2] 美客多毛利卡片已处理 · {month}"
    return "red", f"🔴 [FIN·P0] 美客多毛利月结异常 · {month}"


def build_card(kind: str, summary: dict[str, Any], status_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    period = summary.get("period") or normalize_period(summary.get("month"))[0]
    month = summary.get("month") or period.removeprefix("month_")
    status_fields = status_fields or {}
    template, title = _title_for(kind, month)
    report_url = summary.get("report_url") or _report_url()
    gap_url = summary.get("gap_view_url") or report_url

    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": _plain(title)},
        "elements": [],
    }
    els = card["elements"]

    if kind == "instruction":
        folder_url = os.getenv("CBT_EXPORT_FOLDER_URL", "https://u1wpma3xuhr.feishu.cn/drive/folder/NBPifXvXVl5khXdUSJxcCTvhnWg")
        els.append({"tag": "div", "fields": [
            _field("周期", month),
            _field("截止动作", "7-11号系统自动解析CBT导出"),
            _field("CBT需上传", "Orders / Billing / Ads 三个官方导出文件"),
            _field("本土店", "无需上传，系统通过 ML API / webhook / cache 入表"),
        ]})
        els.append({"tag": "hr"})
        els.append({"tag": "div", "text": _md(
            "本土店覆盖包含 **DISTRIBUIDOR VALMIGOZ**。运营只需要完成 CBT-FULL 三个导出文件上传，后续系统会自动重算成本、审计缺口，并发送下一张处理卡片。"
        )})
        els.append({"tag": "action", "actions": [
            _button("打开上传文件夹", url=folder_url, btn_type="primary"),
            _button("打开毛利报表", url=report_url),
        ]})
        return card

    if kind == "cost_gap":
        els.append({"tag": "div", "fields": [
            _field("报表行数", str(summary.get("report_rows", 0))),
            _field("缺口行数", str(summary.get("gap_row_count", 0))),
            _field("采购缺口", str(summary.get("purchase_gap_count", 0))),
            _field("头程/海外仓缺口", str(summary.get("freight_gap_count", 0))),
            _field("头程合计", _money(float(summary.get("head_total_rmb") or 0))),
            _field("海外仓合计", _money(float(summary.get("ovs_total_rmb") or 0))),
        ]})
        els.append(_top_link_actions(report_url, gap_url))
        els.append({"tag": "hr"})
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in summary.get("gap_rows", [])[:12]:
            grouped[row.get("store") or "未识别店铺"].append(row)
        lines: list[str] = []
        for store, items in grouped.items():
            lines.append(f"**{store}**")
            for row in items:
                gaps = "、".join(row.get("gap_types") or [])
                lines.append(
                    f"- `{row.get('sku')}`：订单 {row.get('orders', 0)}，件数 {row.get('units', 0):g}，营收 {_money(float(row.get('revenue') or 0))}，缺口 {gaps}"
                )
        rest = max(0, int(summary.get("gap_row_count") or 0) - 12)
        if rest:
            lines.append(f"- 其余 {rest} 行请通过下方缺口视图查看")
        els.append({"tag": "hr"})
        store_section = _store_details_section(summary)
        if store_section:
            els.append(store_section)
            els.append({"tag": "hr"})
        els.append({"tag": "div", "text": _md("\n".join(lines) if lines else "未发现可展示的缺口明细。")})
        els.append({"tag": "action", "actions": [
            _button("已补映射，重新核算", action="ml_profit_recalc_cost", period=period, btn_type="primary"),
            _button("本月缺口确认不影响终稿", action="ml_profit_ops_waive_gap", period=period),
            _button("打开缺口视图", url=gap_url),
            _button("打开毛利报表", url=report_url),
        ]})
        return card

    if kind in ("ops_operating", "ops_final"):
        last_recalc = _fmt_ms(status_fields.get("最近重算时间")) or "-"
        delta = _num(status_fields.get("全额毛利差异"))
        data_state = _text(status_fields.get("状态")) or "待运营确认"
        delta_text = _signed_money(delta) if abs(delta) > 0.0001 else "无变化"
        els.append({"tag": "div", "fields": [
            _field("店铺覆盖", str(summary.get("store_count", 0))),
            _field("订单行数", str(summary.get("report_rows", 0))),
            _field("营收", _money(float(summary.get("revenue_rmb") or 0))),
            _field("全额毛利", _money(float(summary.get("gross_profit_rmb") or 0))),
            _field("采购缺口", str(summary.get("purchase_gap_count", 0))),
            _field("成本缺口", str(summary.get("gap_row_count", 0))),
            _field("CBT解析状态", str(summary.get("cbt_state") or "-")),
            _field("最近重算", last_recalc),
            _field("数据状态", data_state),
            _field("较上次重算", delta_text),
        ]})
        els.append(_top_link_actions(report_url))
        els.append({"tag": "hr"})
        store_section = _store_details_section(summary)
        if store_section:
            els.append(store_section)
            els.append({"tag": "hr"})
        if kind == "ops_operating":
            explanation = (
                "系统审计未发现订单、广告、采购成本或头程/海外仓成本硬缺口。"
                "本次只确认**经营暂结数据**，用于运营提成和公司管理毛利；官方账单尚未最终核销。"
                "确认时会再次核对报表版本，内容变化会自动拦截。"
            )
            confirm_label = "确认经营暂结数据"
        else:
            explanation = (
                "系统审计未发现成本缺口，且当前版本已完成官方账单 A/B。"
                "请确认最终核销数据；确认时会再次核对报表版本。"
            )
            confirm_label = "确认最终核销数据"
        els.append({"tag": "div", "text": _md(explanation)})
        els.append({"tag": "action", "actions": [
            _button(confirm_label, action="ml_profit_ops_confirm", period=period, btn_type="primary"),
            _button("发现问题，退回重算", action="ml_profit_ops_reject", period=period, btn_type="danger"),
        ]})
        return card

    if kind == "finance_operating":
        ops_name = _text(status_fields.get("运营确认人")) or "-"
        ops_time = _fmt_ms(status_fields.get("运营确认时间"))
        status_result = _last_result(status_fields)
        due_on = (
            _text(summary.get("final_reconciliation_due_on"))
            or _text(status_result.get("final_reconciliation_due_on"))
            or _final_reconciliation_due(month)[0]
        )
        els.append({"tag": "div", "fields": [
            _field("运营确认人", ops_name),
            _field("运营确认时间", ops_time),
            _field("报表性质", "经营暂结（非最终账单）"),
            _field("最终核销预计", due_on),
            _field("报表行数", str(summary.get("report_rows", 0))),
            _field("营收", _money(float(summary.get("revenue_rmb") or 0))),
            _field("全额毛利", _money(float(summary.get("gross_profit_rmb") or 0))),
            _field("官方账单 A/B", "待最终核销"),
        ]})
        els.append(_top_link_actions(report_url))
        els.append({"tag": "hr"})
        store_section = _store_details_section(summary)
        if store_section:
            els.append(store_section)
            els.append({"tag": "hr"})
        els.append({"tag": "div", "text": _md(
            "请先打开上方**47列财务审核版**（沿用7月定稿格式，含数据源、检查页）。"
            "审核版尚未放行；确认后系统另存并冻结**经营暂结报表**，可用于运营提成和公司毛利汇总。"
            "后续官方账单差异只进入最终核销，不会覆盖本次暂结报表。"
        )})
        els.append({"tag": "action", "actions": [
            _button("放行经营暂结", action="ml_profit_finance_operating_confirm", period=period, btn_type="primary"),
            _button("退回运营复核", action="ml_profit_finance_reject", period=period, btn_type="danger"),
        ]})
        return card

    if kind == "finance_final":
        ops_name = _text(status_fields.get("运营确认人")) or "-"
        ops_time = _fmt_ms(status_fields.get("运营确认时间"))
        els.append({"tag": "div", "fields": [
            _field("运营确认人", ops_name),
            _field("运营确认时间", ops_time),
            _field("终稿版本", month),
            _field("报表行数", str(summary.get("report_rows", 0))),
            _field("营收", _money(float(summary.get("revenue_rmb") or 0))),
            _field("全额毛利", _money(float(summary.get("gross_profit_rmb") or 0))),
        ]})
        els.append(_top_link_actions(report_url))
        els.append({"tag": "hr"})
        store_section = _store_details_section(summary)
        if store_section:
            els.append(store_section)
            els.append({"tag": "hr"})
        operating_url = _text(_last_result(status_fields).get("operating_report_url"))
        note = "此版本已完成官方账单 A/B。财务确认后，月结状态会进入 **财务已确认终稿**。"
        if operating_url:
            note += f"\n\n此前经营暂结报表保持不变：[打开经营暂结报表]({operating_url})。"
        els.append({"tag": "div", "text": _md(note)})
        els.append({"tag": "action", "actions": [
            _button("财务确认终稿", action="ml_profit_finance_confirm", period=period, btn_type="primary"),
            _button("退回运营复核", action="ml_profit_finance_reject", period=period, btn_type="danger"),
        ]})
        return card

    els.append({"tag": "div", "text": _md(str(summary.get("last_error") or "月结流程异常，请查看 ml-sync 日志。"))})
    els.append({"tag": "action", "actions": [_button("打开毛利报表", url=report_url)]})
    return card


def build_processed_card(
    month: str,
    result: str,
    actor: str = "",
    detail: str = "",
    ok: bool = True,
    report_url: str = "",
) -> dict[str, Any]:
    template = "grey" if ok else "red"
    report_url = report_url or _report_url()
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": _plain(f"✅ [FIN·P2] 美客多毛利已处理 · {month}")},
        "elements": [
            {"tag": "div", "fields": [
                _field("处理结果", result),
                _field("处理人", actor or "-"),
                _field("处理时间", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                _field("业务状态", result),
            ]},
            {"tag": "hr"},
            {"tag": "div", "text": _md((detail or "操作已写入月结状态台。") + "\n\n_此卡片已处理，无需重复点击。_")},
            {"tag": "action", "actions": [
                _button("打开飞书毛利报表", url=report_url),
                _button("打开月结状态台", url=_status_url()),
            ]},
        ],
    }


async def send_card(card: dict[str, Any], receive_id: str, receive_id_type: str = "chat_id") -> dict[str, Any]:
    tok = await _tenant_token(CARD_APP_ID, CARD_APP_SECRET)
    return await _fs_json(
        "POST",
        f"{FEISHU}/im/v1/messages?receive_id_type={receive_id_type}",
        tok,
        {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
    )


async def record_advertising_failure(
    period: str,
    shop: str,
    message: str,
    send: bool = True,
) -> dict[str, Any]:
    """Publish a visible fail-closed result without touching report rows."""
    period, month = normalize_period(period=period)
    failure_version = time.time_ns()
    sqlite_marker_error = ""
    fallback_marker_error = ""
    tok: str | None = None
    failed_shops_list = [shop]
    display_message = _ad_failure_message(failed_shops_list)
    status_fields: dict[str, Any] = {}
    status_record: dict[str, Any] | None = None
    status_write_error = ""
    # 先发布可跨进程读取的失败闸，再等月份状态锁。否则财务生成器持锁时，
    # 新失败会排队到终稿写入之后才可见，造成短暂的假成功。
    _EMERGENCY_AD_FAILURES.setdefault(period, {})[shop] = failure_version
    try:
        await db.set_ad_sync_failure(period, shop, message, failed_at=failure_version)
    except Exception as e:
        sqlite_marker_error = f"{type(e).__name__}: {e}"[:500]
    try:
        await db.set_ad_sync_failure_fallback(period, shop, message, failure_version)
    except Exception as e:
        fallback_marker_error = f"{type(e).__name__}: {e}"[:500]

    async with _status_mutation_lock(period):
        try:
            tok = await _tenant_token()
            prior = await _get_status(period, tok)
            failed_shops = set(_failed_ad_shops((prior or {}).get("fields", {})))
            failed_shops.update(_EMERGENCY_AD_FAILURES.get(period, {}))
            failed_shops.add(shop)
            failed_shops_list = sorted(failed_shops)
            display_message = _ad_failure_message(failed_shops_list)
            now_ms = int(time.time() * 1000)
            status_fields = {
                "状态": "异常",
                "最近重算时间": now_ms,
                "最后错误": display_message[:1800],
                "最后结果JSON": json.dumps(
                    {
                        "status": "error",
                        "period": period,
                        "month": month,
                        "failure_type": "advertising_fetch",
                        "failed_ad_shops": failed_shops_list,
                        "latest_failed_shop": shop,
                        "advertising": "抓取失败",
                        "report_rows_changed": False,
                        "detail": message[:500],
                    },
                    ensure_ascii=False,
                ),
            }
            status_record = await _upsert_status(period, status_fields, tok)
        except Exception as e:
            status_write_error = f"{type(e).__name__}: {e}"[:500]
        summary = {
            "status": "error",
            "period": period,
            "month": month,
            "last_error": display_message,
            "report_url": _report_url(),
        }
        card = build_card("error", summary, status_fields)
        result: dict[str, Any] = {
            "status": "error_recorded",
            "period": period,
            "shop": shop,
            "message": display_message,
            "card": card,
            "visible_channels": [],
            "gate_channels": ["emergency_memory"],
        }
        if not sqlite_marker_error:
            result["gate_channels"].append("sqlite")
        else:
            result["sqlite_marker_error"] = sqlite_marker_error
        if not fallback_marker_error:
            result["gate_channels"].append("persistent_fallback")
        else:
            result["fallback_marker_error"] = fallback_marker_error
        if status_record:
            result["status_record"] = status_record
            result["visible_channels"].append("status_ledger")
            result["gate_channels"].append("status_ledger")
        if status_write_error:
            result["status_write_error"] = status_write_error

        card_send_error = ""
        if send:
            try:
                sent = await send_card(card, ML_GROUP_ID)
                result["send_result"] = sent
                result["visible_channels"].append("error_card")
                msg_id = _message_id(sent)
                if msg_id:
                    result["message_id"] = msg_id
                    if tok:
                        try:
                            await _upsert_status(period, {"最后卡片 message_id": msg_id}, tok)
                        except Exception as e:
                            result["message_id_write_error"] = f"{type(e).__name__}: {e}"[:500]
            except Exception as e:
                card_send_error = f"{type(e).__name__}: {e}"[:500]
                result["card_send_error"] = card_send_error

        if not result["visible_channels"]:
            raise RuntimeError(
                "advertising failure could not be published: "
                f"status={status_write_error or 'not requested'} card={card_send_error or 'not requested'}"
            )
        persistent_gates = {"sqlite", "persistent_fallback", "status_ledger"}
        if not persistent_gates.intersection(result["gate_channels"]):
            raise RuntimeError("advertising failure has no persistent confirmation gate")
        return result


async def clear_advertising_failure(
    period: str,
    shop: str,
    success_started_at: int | None = None,
) -> dict[str, Any]:
    """Resolve one shop's failure only after its report rows were verified."""
    period, month = normalize_period(period=period)
    tok = await _tenant_token()
    async with _status_mutation_lock(period):
        marker_versions: dict[str, int] = {}
        for row in (
            await db.list_ad_sync_failures(period)
            + await db.list_ad_sync_failure_fallbacks(period)
        ):
            marker_shop = _text(row.get("shop")).strip()
            if marker_shop:
                marker_versions[marker_shop] = max(
                    marker_versions.get(marker_shop, 0),
                    int(row.get("failed_at") or 0),
                )
        for marker_shop, failed_at in _EMERGENCY_AD_FAILURES.get(period, {}).items():
            marker_versions[marker_shop] = max(marker_versions.get(marker_shop, 0), failed_at)

        prior = await _get_status(period, tok)
        failed_shops = set(_failed_ad_shops((prior or {}).get("fields", {})))
        failed_shops.update(marker_versions)
        latest_failure = marker_versions.get(shop, 0)
        if success_started_at is not None and latest_failure > success_started_at:
            return {
                "status": "superseded_by_newer_failure",
                "period": period,
                "shop": shop,
                "failed_ad_shops": sorted(failed_shops),
            }
        if shop not in failed_shops:
            return {"status": "unchanged", "period": period, "shop": shop, "failed_ad_shops": sorted(failed_shops)}

        failed_shops.remove(shop)
        remaining = sorted(failed_shops)
        state = "异常" if remaining else "退回重算"
        last_error = _ad_failure_message(remaining) if remaining else ""
        fields = {
            "状态": state,
            "最后错误": last_error,
            "最后结果JSON": json.dumps(
                {
                    "status": "resolved" if not remaining else "error",
                    "period": period,
                    "month": month,
                    "failure_type": "advertising_fetch" if remaining else "",
                    "failed_ad_shops": remaining,
                    "latest_resolved_shop": shop,
                },
                ensure_ascii=False,
            ),
        }
        status_record = await _upsert_status(period, fields, tok)

        if success_started_at is None:
            await db.clear_ad_sync_failure_fallback(period, shop)
            await db.clear_ad_sync_failure(period, shop)
        else:
            await db.clear_ad_sync_failure_fallback(period, shop, not_after=success_started_at)
            await db.clear_ad_sync_failure(period, shop, not_after=success_started_at)
        emergency_version = _EMERGENCY_AD_FAILURES.get(period, {}).get(shop)
        if emergency_version is not None and (
            success_started_at is None or emergency_version <= success_started_at
        ):
            _EMERGENCY_AD_FAILURES.get(period, {}).pop(shop, None)
            if not _EMERGENCY_AD_FAILURES.get(period):
                _EMERGENCY_AD_FAILURES.pop(period, None)
    return {
        "status": "cleared" if not remaining else "partially_cleared",
        "period": period,
        "shop": shop,
        "failed_ad_shops": remaining,
        "status_record": status_record,
    }


def _message_id(resp: dict[str, Any]) -> str:
    return (
        resp.get("data", {}).get("message_id")
        or resp.get("data", {}).get("message", {}).get("message_id")
        or ""
    )


async def patch_card(message_id: str, card: dict[str, Any]) -> dict[str, Any]:
    tok = await _tenant_token(CARD_APP_ID, CARD_APP_SECRET)
    return await _fs_json(
        "PATCH",
        f"{FEISHU}/im/v1/messages/{message_id}",
        tok,
        {"content": json.dumps(card, ensure_ascii=False)},
    )


async def patch_or_fallback(message_id: str, card: dict[str, Any], chat_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"patched": False, "fallback_sent": False}
    if message_id:
        try:
            result["patch_result"] = await patch_card(message_id, card)
            result["patched"] = True
            return result
        except Exception as e:
            result["patch_error"] = f"{type(e).__name__}: {e}"
    if chat_id:
        try:
            sent = await send_card(card, chat_id)
            result["fallback_sent"] = True
            result["fallback_message_id"] = _message_id(sent)
        except Exception as e:
            result["fallback_error"] = f"{type(e).__name__}: {e}"
    return result


async def _prepare_finance_review(summary: dict[str, Any]) -> dict[str, Any]:
    """Create the approved-format review copy without granting financial approval."""
    from app import unified_report

    source_hash = _text(summary.get("report_hash"))
    if not source_hash or not summary.get("operating_ready"):
        raise ValueError("当前数据未通过经营审核检查，不能生成财务审核版")
    report = await unified_report.generate(
        summary["period"], commit=True, expected_source_hash=source_hash, close_mode="review"
    )
    latest = await audit(period=summary["period"], commit=False, run_cost_preview=False)
    if not latest.get("operating_ready") or _text(latest.get("report_hash")) != source_hash:
        raise ValueError("审核版生成期间数据变化，请重新生成审核版")
    if not report.get("spreadsheet_token"):
        raise ValueError("财务审核版缺少电子表格地址")
    status = await _get_status(summary["period"]) or {}
    fields = status.get("fields") or {}
    result = _last_result(fields)
    result.update({"review_source_hash": source_hash,
                   "review_report_url": f"https://u1wpma3xuhr.feishu.cn/sheets/{report['spreadsheet_token']}"})
    await _upsert_status(summary["period"], {"最后结果JSON": json.dumps(result, ensure_ascii=False)})
    return {**summary, "report_url": f"https://u1wpma3xuhr.feishu.cn/sheets/{report['spreadsheet_token']}",
            "review_source_hash": source_hash}


def _bind_review_hash(card: dict[str, Any], summary: dict[str, Any]) -> None:
    for element in card.get("elements", []):
        for button in element.get("actions", []):
            value = button.get("value", {})
            if value.get("action") == "ml_profit_finance_operating_confirm" and summary.get("review_source_hash"):
                value["review_source_hash"] = summary["review_source_hash"]


async def card_endpoint(
    kind: str | None = None,
    month: str | None = None,
    period: str | None = None,
    send: bool = False,
    receive_id: str | None = None,
    receive_id_type: str = "chat_id",
    refresh_message_id: str | None = None,
) -> dict[str, Any]:
    if refresh_message_id and (send or kind != "finance_operating"):
        raise ValueError("更新原卡仅支持财务经营暂结审核，且不能同时发送新卡")
    p, normalized_month = normalize_period(month, period)
    summary: dict[str, Any] | None = None
    async with _status_mutation_lock(p):
        early_status = await _get_status(p)
        early_fields = early_status.get("fields", {}) if early_status else {}
        early_state = _text(early_fields.get("状态")) if early_fields else ""
        initial_marker_error = ""
        try:
            initial_failed_shops = await _open_ad_failures(p, early_fields)
        except Exception as e:
            initial_failed_shops = _failed_ad_shops(early_fields)
            initial_marker_error = f"广告失败状态读取失败：{type(e).__name__}"

        if initial_failed_shops or initial_marker_error:
            initial_reason = _with_ad_failure(initial_marker_error, initial_failed_shops)
            summary = {
                "status": "error",
                "period": p,
                "month": normalized_month,
                "state": "异常",
                "next_card": "error",
                "last_error": initial_reason,
                "failed_ad_shops": initial_failed_shops,
                "report_url": _report_url(),
            }
        elif kind in ("none", "skip"):
            return {"status": "skipped", "reason": "no_next_card", "kind": kind, "period": p}
        elif kind is None and early_state in ("运营已确认", "财务已确认终稿"):
            return {"status": "skipped", "reason": "already_confirmed", "state": early_state, "period": p}

    if summary is None:
        summary = await audit(month=month, period=period, commit=False, run_cost_preview=(kind != "instruction"))

    requested_kind = kind
    kind = "error" if summary.get("next_card") == "error" else (kind or summary.get("next_card") or "instruction")
    if kind == "finance_final" and not summary.get("ab_verified"):
        return {
            "status": "skipped",
            "reason": "ab_not_verified",
            "kind": "none",
            "period": summary["period"],
            "summary": summary,
        }
    async with _status_mutation_lock(summary["period"]):
        status = await _get_status(summary["period"]) or {}
        status_fields = status.get("fields") if status else {}
        final_marker_error = ""
        try:
            final_failed_shops = await _open_ad_failures(summary["period"], status_fields)
        except Exception as e:
            final_failed_shops = _failed_ad_shops(status_fields)
            final_marker_error = f"广告失败状态读取失败：{type(e).__name__}"
        if final_failed_shops or final_marker_error:
            final_reason = _with_ad_failure(final_marker_error, final_failed_shops)
            summary.update({
                "status": "error",
                "state": "异常",
                "next_card": "error",
                "last_error": final_reason,
                "failed_ad_shops": final_failed_shops,
            })
            kind = "error"
        current_state = _text(status_fields.get("状态")) if status_fields else ""
        if (
            requested_kind is None
            and summary.get("next_card") != "error"
            and current_state in ("运营已确认", "财务已确认暂结", "财务已确认终稿")
            and summary.get("next_card") in ("none", "skip", None)
        ):
            return {
                "status": "skipped",
                "reason": "already_confirmed",
                "state": current_state,
                "period": summary["period"],
                "summary": summary,
            }
        if refresh_message_id and refresh_message_id != _text(status_fields.get("最后卡片 message_id")):
            raise ValueError("目标已不是当前审核卡，禁止覆盖旧卡")
        if kind == "finance_operating" and (send or refresh_message_id):
            if current_state != "运营已确认":
                raise ValueError("请先完成运营确认，再生成财务审核卡")
            summary = await _prepare_finance_review(summary)
        card = build_card(kind, summary, status_fields)
        _bind_review_hash(card, summary)
        out = {"status": "ok", "kind": kind, "period": summary["period"], "summary": summary, "card": card}
        if refresh_message_id:
            if kind != "finance_operating":
                raise ValueError("当前存在异常，不能更新财务审核卡")
            out["patch_result"] = await patch_card(refresh_message_id, card)
            out["message_id"] = refresh_message_id
        if send:
            target = receive_id or (
                FINANCE_GROUP_ID
                if kind in ("finance_operating", "finance_final")
                else ML_GROUP_ID
            )
            sent = await send_card(card, target, receive_id_type)
            msg_id = (
                sent.get("data", {}).get("message_id")
                or sent.get("data", {}).get("message", {}).get("message_id")
            )
            out["send_result"] = sent
            if msg_id:
                tok = await _tenant_token()
                await _upsert_status(summary["period"], {"最后卡片 message_id": msg_id}, tok)
    return out


async def status_endpoint(month: str | None = None, period: str | None = None) -> dict[str, Any]:
    period, month = normalize_period(month, period)
    status = await _get_status(period)
    fields = status.get("fields", {}) if status else {}
    marker_error = ""
    try:
        failed_ad_shops = await _open_ad_failures(period, fields)
    except Exception as e:
        failed_ad_shops = _failed_ad_shops(fields)
        marker_error = f"广告失败状态读取失败：{type(e).__name__}"
    state = "异常" if failed_ad_shops or marker_error else (_text(fields.get("状态")) if fields else "待数据同步")
    result = _last_result(fields)
    report_hash = _text(result.get("report_hash"))
    ab_verified = _resolve_ab_verified(fields, None, report_hash)
    operating_close_confirmed = bool(result.get("operating_close_confirmed"))
    operating_report_hash = _text(result.get("operating_report_hash"))
    operating_snapshot_current = bool(
        operating_report_hash and report_hash and operating_report_hash == report_hash
    )
    operating_ready = (
        operating_close_confirmed
        and operating_snapshot_current
        and not failed_ad_shops
        and not marker_error
    )
    return {
        "status": "ok",
        "period": period,
        "month": month,
        "state": state,
        "ready_for_finance": operating_ready or state == "财务已确认终稿",
        "ready_for_management": operating_ready or state == "财务已确认终稿",
        "ready_for_commission": operating_ready or state == "财务已确认终稿",
        "ready_for_final_reconciliation": ab_verified and state in (
            "运营已确认", "财务已确认暂结", "待最终核销", "财务已确认终稿"
        ),
        "ab_verified": ab_verified,
        "operating_close_confirmed": operating_close_confirmed,
        "operating_snapshot_current": operating_snapshot_current,
        "operating_report_hash": operating_report_hash,
        "operating_report_url": _text(result.get("operating_report_url")),
        "operating_report_sheet_url": _text(result.get("operating_report_sheet_url")),
        "final_reconciliation_due_on": (
            _text(result.get("final_reconciliation_due_on"))
            or _final_reconciliation_due(month)[0]
        ),
        "report_hash": report_hash,
        "failed_ad_shops": failed_ad_shops,
        "marker_error": marker_error,
        "record": status,
    }


async def recalc_cost(
    month: str | None = None,
    period: str | None = None,
    commit: bool = True,
    audit_commit: bool = True,
    ab_verified: bool | None = None,
) -> dict[str, Any]:
    period, month = normalize_period(month, period)
    if commit:
        await invalidate_ab_verification(period, "cost_recalc")
    cost = await anyio.to_thread.run_sync(meitong_cost.run, period, 12, commit)
    summary = await audit(
        period=period,
        commit=audit_commit,
        run_cost_preview=False,
        cost_summary=cost,
        ab_verified=ab_verified,
    )
    return {"status": "ok", "period": period, "month": month, "cost_summary": cost, "audit": summary}


async def confirm_action(payload: dict[str, Any]) -> dict[str, Any]:
    action_context: dict[str, Any] = {}
    try:
        return await _confirm_action_impl(payload, action_context)
    except Exception as exc:
        if action_context.get("claimed"):
            try:
                await db.fail_ml_close_action(
                    action_context["period"],
                    action_context["action_key"],
                    action_context["owner"],
                    f"unhandled action failure: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        raise


async def _confirm_action_impl(
    payload: dict[str, Any],
    action_context: dict[str, Any],
) -> dict[str, Any]:
    action = payload.get("action") or payload.get("value", {}).get("action")
    period, month = normalize_period(payload.get("month"), payload.get("period") or payload.get("value", {}).get("period"))
    actor = _text(payload.get("operator_name") or payload.get("operator_id") or payload.get("open_id") or payload.get("user_id"))
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    message_id = (
        payload.get("message_id")
        or payload.get("open_message_id")
        or payload.get("card_open_message_id")
        or context.get("open_message_id")
        or context.get("message_id")
        or ""
    )
    chat_id = payload.get("open_chat_id") or payload.get("chat_id") or context.get("open_chat_id") or context.get("chat_id") or ""
    patch = payload.get("patch_message", True) is not False
    now_ms = int(time.time() * 1000)
    action_key = f"{message_id or period}:{action}"
    state_mutating_actions = {
        "ml_profit_recalc_cost",
        "ml_profit_ops_confirm",
        "ml_profit_ops_waive_gap",
        "ml_profit_ops_reject",
        "ml_profit_finance_operating_confirm",
        "ml_profit_finance_confirm",
        "ml_profit_finance_reject",
    }
    confirmation_actions = {
        "ml_profit_ops_confirm",
        "ml_profit_ops_waive_gap",
        "ml_profit_finance_operating_confirm",
        "ml_profit_finance_confirm",
    }
    final_ab_actions = {
        "ml_profit_ops_waive_gap",
        "ml_profit_finance_confirm",
    }
    action_owner = uuid.uuid4().hex
    action_claimed = False
    persistent_deduped = False
    persistent_processing = False
    persistent_superseded = False
    tok: str | None = None

    async def _blocked_confirmation(
        reason: str,
        discard_claim: bool = False,
    ) -> dict[str, Any]:
        if action_claimed:
            try:
                if discard_claim:
                    await db.discard_ml_close_action(
                        period, action_key, action_owner
                    )
                else:
                    await db.fail_ml_close_action(
                        period, action_key, action_owner, reason
                    )
            except Exception as exc:
                reason = (
                    f"{reason}；动作台账失败状态写入失败：{type(exc).__name__}"
                )
        processed = build_processed_card(month, "确认已拦截", actor, reason, ok=False)
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {
            "status": "blocked",
            "action": action,
            "period": period,
            "state": "异常",
            "reason": reason,
            "processed_card": processed,
            "feedback": feedback,
        }

    # Reject known-invalid callbacks before allocating an action sequence. An
    # old card must not receive a higher SQLite id and supersede the current,
    # valid action merely because its callback arrived late.
    if action in state_mutating_actions:
        if action in confirmation_actions:
            try:
                active_work = await db.get_active_ml_close_month_work(period)
            except Exception as exc:
                return await _blocked_confirmation(
                    f"成本重算状态读取失败：{type(exc).__name__}"
                )
            if active_work:
                return await _blocked_confirmation(
                    "本月成本正在重算，确认已暂缓；请等待重算完成后再次点击。"
                )
        try:
            tok = await _tenant_token()
            pre_status = await _get_status(period, tok) or {}
        except Exception as exc:
            return await _blocked_confirmation(
                f"月结状态预检失败：{type(exc).__name__}"
            )
        pre_fields = pre_status.get("fields") or {}
        pre_message_id = _text(pre_fields.get("最后卡片 message_id"))
        if message_id and pre_message_id and message_id != pre_message_id:
            return await _blocked_confirmation(
                "该卡片已不是当前月份的最新操作卡，本次动作已拦截。"
            )
        pre_state = _text(pre_fields.get("状态"))
        pre_result = _last_result(pre_fields)
        if (
            action == "ml_profit_finance_operating_confirm"
            and pre_result.get("operating_close_confirmed")
        ):
            report_url = _text(pre_result.get("operating_report_url"))
            sheet_url = _text(pre_result.get("operating_report_sheet_url"))
            processed = build_processed_card(
                month,
                "财务已确认暂结",
                actor,
                "经营暂结报表已经冻结；重复操作不会覆盖原快照。",
                report_url=report_url,
            )
            feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
            return {
                "status": "ok",
                "action": action,
                "period": period,
                "state": "财务已确认暂结",
                "deduped": True,
                "report": {"url": report_url, "sheet_url": sheet_url},
                "processed_card": processed,
                "feedback": feedback,
            }
        if (
            pre_state == "财务已确认终稿"
            and action != "ml_profit_finance_confirm"
        ):
            return await _blocked_confirmation(
                "本月已由财务确认终稿，旧卡片不能再修改终稿状态。"
            )
        if action in confirmation_actions:
            try:
                pre_failed = await _open_ad_failures(period, pre_fields)
            except Exception as exc:
                return await _blocked_confirmation(
                    f"月结确认门禁读取失败：{type(exc).__name__}"
                )
            if pre_failed:
                return await _blocked_confirmation(_ad_failure_message(pre_failed))
        if action == "ml_profit_finance_confirm":
            retrying_generator_error = (
                pre_state == "异常"
                and _text(pre_fields.get("最后错误")).startswith(
                    ("月报生成失败：", "月报终态保护失败：")
                )
            )
            if pre_state not in (
                "运营已确认", "财务已确认终稿"
            ) and not retrying_generator_error:
                return await _blocked_confirmation(
                    f"当前月结状态为“{pre_state or '未知'}”，请先完成运营确认。"
                )
        if action == "ml_profit_finance_operating_confirm" and pre_state not in (
            "运营已确认", "财务已确认暂结"
        ):
            return await _blocked_confirmation(
                f"当前月结状态为“{pre_state or '未知'}”，请先完成运营确认。"
            )
        if action in final_ab_actions and not _resolve_ab_verified(pre_fields, None):
            return await _blocked_confirmation(
                "A/B 对账尚未完成，本次确认已拦截。"
            )
        try:
            action_claim = await db.claim_ml_close_action(
                period, action_key, action_owner
            )
        except Exception as exc:
            reason = f"月结动作去重台账不可用：{type(exc).__name__}"
            processed = build_processed_card(month, "确认已拦截", actor, reason, ok=False)
            feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
            return {
                "status": "blocked",
                "action": action,
                "period": period,
                "state": "异常",
                "reason": reason,
                "processed_card": processed,
                "feedback": feedback,
            }
        action_claimed = bool(action_claim.get("claimed"))
        action_claim_status = _text(action_claim.get("status"))
        persistent_deduped = not action_claimed and action_claim_status == "completed"
        persistent_processing = not action_claimed and action_claim_status == "processing"
        persistent_superseded = not action_claimed and action_claim_status == "superseded"
        if action_claimed:
            action_context.update({
                "claimed": True,
                "period": period,
                "action_key": action_key,
                "owner": action_owner,
            })

    action_epoch = _ACTION_EPOCHS.get(period, 0)
    is_new_action_key = action_claimed
    if action_claimed:
        keyed_epochs = _ACTION_KEY_EPOCHS.setdefault(period, {})
        action_epoch += 1
        _ACTION_EPOCHS[period] = action_epoch
        keyed_epochs[action_key] = action_epoch
    if action_claimed and action in confirmation_actions:
        try:
            active_work = await db.get_active_ml_close_month_work(period)
        except Exception as exc:
            return await _blocked_confirmation(
                f"成本重算状态读取失败：{type(exc).__name__}"
            )
        if active_work:
            return await _blocked_confirmation(
                "本月成本正在重算，确认已暂缓；请等待重算完成后再次点击。",
                discard_claim=True,
            )
    if tok is None:
        try:
            tok = await _tenant_token()
        except Exception as exc:
            return await _blocked_confirmation(
                f"月结状态授权失败：{type(exc).__name__}"
            )

    # Reject/recalculate must publish cancellation before waiting on the local
    # month lock, but first reject a known stale card so an old callback cannot
    # cancel an already approved report after a deployment has created a fresh
    # (initially empty) action ledger.
    if is_new_action_key and action in {
        "ml_profit_recalc_cost",
        "ml_profit_ops_reject",
        "ml_profit_finance_reject",
    }:
        try:
            pre_status = await _get_status(period, tok) or {}
        except Exception as exc:
            return await _blocked_confirmation(
                f"月结状态读取失败：{type(exc).__name__}"
            )
        pre_fields = pre_status.get("fields") or {}
        pre_message_id = _text(pre_fields.get("最后卡片 message_id"))
        if message_id and pre_message_id and message_id != pre_message_id:
            return await _blocked_confirmation(
                "该卡片已不是当前月份的最新操作卡，本次动作已拦截。",
                discard_claim=True,
            )
        if _text(pre_fields.get("状态")) == "财务已确认终稿":
            return await _blocked_confirmation(
                "本月已由财务确认终稿，旧卡片不能取消或退回终稿。",
                discard_claim=True,
            )
        try:
            await db.cancel_unified_report_generation(
                period, f"close action requested: {action}"
            )
            await db.cancel_unified_report_generation(
                f"{period}::operating", f"close action requested: {action}"
            )
        except Exception as exc:
            return await _blocked_confirmation(
                f"月报生成取消失败：{type(exc).__name__}"
            )

    block_reason = ""
    discard_claim = False
    deduped = False
    in_progress = False
    async with _status_mutation_lock(period):
        current = await _get_status(period, tok) or {}
        current_fields = current.get("fields") or {}
        if action in confirmation_actions:
            if action in final_ab_actions and not _resolve_ab_verified(current_fields, None):
                block_reason = "A/B 对账尚未完成，本次确认已拦截。"
            try:
                current_failed = await _open_ad_failures(period, current_fields)
            except Exception as e:
                current_failed = []
                block_reason = f"广告失败状态读取失败：{type(e).__name__}"
            if current_failed:
                block_reason = _ad_failure_message(current_failed)
        if not block_reason and action == "ml_profit_finance_confirm":
            current_state = _text(current_fields.get("状态"))
            retrying_generator_error = (
                current_state == "异常"
                and _text(current_fields.get("最后错误")).startswith(
                    ("月报生成失败：", "月报终态保护失败：")
                )
            )
            if current_state not in (
                "运营已确认", "财务已确认终稿"
            ) and not retrying_generator_error:
                block_reason = f"当前月结状态为“{current_state or '未知'}”，请先完成运营确认。"
                discard_claim = True
        if not block_reason and action == "ml_profit_finance_operating_confirm":
            current_state = _text(current_fields.get("状态"))
            if current_state not in ("运营已确认", "财务已确认暂结"):
                block_reason = f"当前月结状态为“{current_state or '未知'}”，请先完成运营确认。"
                discard_claim = True
        current_message_id = _text(current_fields.get("最后卡片 message_id"))
        if (
            not block_reason
            and action in state_mutating_actions
            and message_id
            and current_message_id
            and message_id != current_message_id
        ):
            block_reason = "该卡片已不是当前月份的最新操作卡，本次动作已拦截。"
            discard_claim = True
        if not block_reason and persistent_deduped:
            deduped = True
        elif not block_reason and persistent_processing:
            in_progress = True
        elif not block_reason and persistent_superseded:
            block_reason = "该卡片动作已被更新的月结操作取代，本次执行已拦截。"
        elif (
            not block_reason
            and action not in state_mutating_actions
            and action
            and _text(current_fields.get("最后按钮动作Key")) == action_key
        ):
            deduped = True
        elif not block_reason and action not in state_mutating_actions and action:
            await _upsert_status(period, {"最后按钮动作Key": action_key, "最后按钮动作时间": now_ms}, tok)

    if block_reason:
        return await _blocked_confirmation(
            block_reason,
            discard_claim=discard_claim,
        )
    if in_progress:
        processed = build_processed_card(
            month,
            "处理中",
            actor,
            "该操作仍在执行，请稍后查看结果，不要重复点击。",
        )
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {
            "status": "processing",
            "action": action,
            "period": period,
            "deduped": True,
            "processed_card": processed,
            "feedback": feedback,
        }
    if deduped:
        processed = build_processed_card(month, "已处理", actor, "重复点击已拦截，未重复执行")
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {"status": "ok", "action": action, "period": period, "deduped": True, "processed_card": processed, "feedback": feedback}

    if action == "ml_profit_recalc_cost":
        try:
            work_claim = await db.claim_ml_close_month_work(
                period,
                "cost_recalc",
                action_key,
                action_owner,
            )
        except Exception as exc:
            return await _blocked_confirmation(
                f"成本重算占位失败：{type(exc).__name__}"
            )
        if not work_claim.get("claimed"):
            return await _blocked_confirmation(
                "本次重算已被更新的月结操作取代，未开始写入成本。"
            )
        try:
            recalc = await recalc_cost(period=period, commit=True, audit_commit=False)
        except Exception as exc:
            await db.finish_ml_close_month_work(
                period,
                action_owner,
                "failed",
                f"{type(exc).__name__}: {exc}",
            )
            raise
        await db.finish_ml_close_month_work(
            period,
            action_owner,
            "completed",
        )
        kind = recalc["audit"].get("next_card")
        next_card: dict[str, Any] = {}
        processed: dict[str, Any] = {}
        sent: dict[str, Any] = {}
        async with _status_mutation_lock(period):
            async with db.ml_close_action_finalization_guard(
                period, action_key, action_owner
            ) as latest_action:
                if not latest_action:
                    block_reason = "重算期间收到新的月结操作，本次重算状态写入已拦截。"
                else:
                    guard_status = await _get_status(period, tok) or {}
                    guard_fields = guard_status.get("fields") or {}
                    if _text(guard_fields.get("状态")) == "财务已确认终稿":
                        block_reason = "本月已由财务确认终稿，本次重算状态写入已拦截。"
                    else:
                        await _commit_audit_snapshot(
                            recalc["audit"],
                            tok,
                            _text(recalc["audit"].get("_base_error")),
                            {
                                "最后按钮动作Key": action_key,
                                "最后按钮动作时间": now_ms,
                            },
                        )
                        kind = recalc["audit"].get("next_card")
                        next_card = build_card(kind, recalc["audit"])
                        processed = build_processed_card(
                            month,
                            "已重新核算成本",
                            actor,
                            f"下一步卡片：{kind}",
                        )
                        sent = await send_card(next_card, ML_GROUP_ID)
                        sent_id = _message_id(sent)
                        if sent_id:
                            await _upsert_status(period, {"最后卡片 message_id": sent_id}, tok)
        if block_reason:
            return await _blocked_confirmation(block_reason)
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {"status": "ok", "action": action, "period": period, "recalc": recalc, "processed_card": processed, "feedback": feedback, "next_card": next_card, "next_kind": kind, "send_result": sent}

    summary = await audit(period=period, commit=False, run_cost_preview=False)

    if action in confirmation_actions and summary.get("next_card") == "error":
        reason = _text(summary.get("last_error")) or "月结存在未解决异常，确认已拦截。"
        return await _blocked_confirmation(reason)
    approved_report_hash = _text(summary.get("report_hash"))
    if action == "ml_profit_finance_operating_confirm":
        reviewed_hash = payload.get("review_source_hash") or (payload.get("value") or {}).get("review_source_hash")
        published_review = _last_result((await _get_status(period, tok) or {}).get("fields") or {})
        if (not reviewed_hash or reviewed_hash != approved_report_hash
                or reviewed_hash != published_review.get("review_source_hash")
                or not published_review.get("review_report_url")):
            return await _blocked_confirmation("审核版缺失或数据已变化，请打开更新后的47列审核卡再确认。")
    if action in final_ab_actions and (
        summary.get("ab_verified") is not True or not approved_report_hash
    ):
        return await _blocked_confirmation(
            "当前报表版本尚未完成 A/B 对账，本次确认已拦截。"
        )

    if action in ("ml_profit_ops_confirm", "ml_profit_ops_waive_gap"):
        state = "运营已确认"
        detail = (
            "运营确认最终核销数据"
            if summary.get("ab_verified")
            else "运营确认经营暂结数据"
        ) if action == "ml_profit_ops_confirm" else "运营确认本月缺口不影响终稿"
        status_update = {
            "状态": state,
            "运营确认人": actor,
            "运营确认时间": now_ms,
            "最后按钮动作Key": action_key,
            "最后按钮动作时间": now_ms,
        }
        block_reason = ""
        finance_card: dict[str, Any] = {}
        sent: dict[str, Any] = {}
        review_summary: dict[str, Any] = {}
        async with _status_mutation_lock(period):
            latest = await _get_status(period, tok) or {}
            latest_fields = latest.get("fields") or {}
            try:
                latest_failed = await _open_ad_failures(period, latest_fields)
            except Exception as e:
                latest_failed = []
                block_reason = f"广告失败状态读取失败：{type(e).__name__}"
            if latest_failed:
                block_reason = _ad_failure_message(latest_failed)
            if not block_reason and _text(latest_fields.get("状态")) == "财务已确认终稿":
                block_reason = "本月已由财务确认终稿，旧运营卡片不能覆盖终稿状态。"
            if not block_reason and _ACTION_EPOCHS.get(period) != action_epoch:
                block_reason = "确认期间收到新的月结操作，本次运营确认已拦截。"
            if not block_reason and not summary.get("ab_verified"):
                review_summary = await _prepare_finance_review(summary)
            if not block_reason:
                async with db.ml_close_action_finalization_guard(
                    period, action_key, action_owner
                ) as latest_action:
                    if not latest_action:
                        block_reason = "确认期间收到新的月结操作，本次运营确认已拦截。"
                    else:
                        guard_status = await _get_status(period, tok) or {}
                        guard_fields = guard_status.get("fields") or {}
                        if _text(guard_fields.get("状态")) == "财务已确认终稿":
                            block_reason = "本月已由财务确认终稿，旧运营卡片不能覆盖终稿状态。"
                        if not block_reason:
                            live_summary = await audit(
                                period=period,
                                commit=False,
                                run_cost_preview=False,
                            )
                            if _text(live_summary.get("report_hash")) != approved_report_hash:
                                block_reason = "确认期间报表内容已变化，当前版本需重新确认。"
                            elif (
                                action == "ml_profit_ops_confirm"
                                and live_summary.get("ab_verified") is not True
                                and not live_summary.get("operating_ready")
                            ):
                                block_reason = "当前报表仍有订单、广告或成本硬缺口，经营暂结确认已拦截。"
                            elif action == "ml_profit_ops_waive_gap" and live_summary.get("ab_verified") is not True:
                                block_reason = "缺口豁免只适用于最终核销，经营暂结不能带成本缺口放行。"
                        if not block_reason:
                            await _upsert_status(period, status_update, tok)
                            status = await _get_status(period, tok) or {}
                            finance_kind = (
                                "finance_final"
                                if live_summary.get("ab_verified")
                                else "finance_operating"
                            )
                            if finance_kind == "finance_operating":
                                live_summary = {**live_summary, "report_url": review_summary["report_url"],
                                                "review_source_hash": review_summary["review_source_hash"]}
                            finance_card = build_card(finance_kind, live_summary, status.get("fields") or {})
                            _bind_review_hash(finance_card, live_summary)
                            sent = await send_card(finance_card, FINANCE_GROUP_ID)
                            sent_id = _message_id(sent)
                            if sent_id:
                                await _upsert_status(period, {"最后卡片 message_id": sent_id}, tok)
        if block_reason:
            return await _blocked_confirmation(block_reason)
        processed = build_processed_card(month, state, actor, detail)
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {"status": "ok", "action": action, "period": period, "state": state, "processed_card": processed, "feedback": feedback, "next_card": finance_card, "next_kind": finance_kind, "send_result": sent}

    if action == "ml_profit_ops_reject":
        async with _status_mutation_lock(period):
            latest = await _get_status(period, tok) or {}
            latest_fields = latest.get("fields") or {}
            if _text(latest_fields.get("状态")) == "财务已确认终稿":
                block_reason = "本月已由财务确认终稿，旧运营卡片不能退回终稿。"
            if not block_reason and _ACTION_EPOCHS.get(period) != action_epoch:
                block_reason = "退回期间收到新的月结操作，本次运营退回已拦截。"
            if not block_reason:
                async with db.ml_close_action_finalization_guard(
                    period, action_key, action_owner
                ) as latest_action:
                    if not latest_action:
                        block_reason = "退回期间收到新的月结操作，本次运营退回已拦截。"
                    else:
                        guard_status = await _get_status(period, tok) or {}
                        guard_fields = guard_status.get("fields") or {}
                        if _text(guard_fields.get("状态")) == "财务已确认终稿":
                            block_reason = "本月已由财务确认终稿，旧运营卡片不能退回终稿。"
                        else:
                            await _upsert_status(
                                period,
                                {
                                    "状态": "退回重算",
                                    "最后错误": "运营退回重算",
                                    "最后按钮动作Key": action_key,
                                    "最后按钮动作时间": now_ms,
                                },
                                tok,
                            )
        if block_reason:
            return await _blocked_confirmation(block_reason)
        processed = build_processed_card(month, "退回重算", actor, "运营发现问题，需补数后重新核算", ok=False)
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {"status": "ok", "action": action, "period": period, "state": "退回重算", "processed_card": processed, "feedback": feedback}

    if action == "ml_profit_finance_operating_confirm":
        from app import unified_report

        block_reason = ""
        report: dict[str, Any] = {}
        async with _status_mutation_lock(period):
            latest = await _get_status(period, tok) or {}
            latest_fields = latest.get("fields") or {}
            try:
                latest_failed = await _open_ad_failures(period, latest_fields)
            except Exception as exc:
                latest_failed = []
                block_reason = f"广告失败状态读取失败：{type(exc).__name__}"
            if latest_failed:
                block_reason = _ad_failure_message(latest_failed)
            latest_state = _text(latest_fields.get("状态"))
            if not block_reason and latest_state not in ("运营已确认", "财务已确认暂结"):
                block_reason = f"当前月结状态为“{latest_state or '未知'}”，请先完成运营确认。"
            if not block_reason and _ACTION_EPOCHS.get(period) != action_epoch:
                block_reason = "生成前收到新的月结操作，本次经营暂结已拦截。"
            if not block_reason:
                live_before = await audit(
                    period=period, commit=False, run_cost_preview=False
                )
                if not live_before.get("operating_ready"):
                    block_reason = "当前报表仍有订单、广告或成本硬缺口，经营暂结已拦截。"
                elif _text(live_before.get("report_hash")) != approved_report_hash:
                    block_reason = "生成前报表内容已变化，当前经营暂结版本需重新确认。"

            if not block_reason:
                try:
                    report = await unified_report.generate(
                        period,
                        commit=True,
                        expected_source_hash=approved_report_hash,
                        close_mode="operating",
                    )
                except Exception as exc:
                    block_reason = f"经营暂结报表生成失败：{exc}"

            if not block_reason:
                live_after = await audit(
                    period=period, commit=False, run_cost_preview=False
                )
                if not live_after.get("operating_ready"):
                    block_reason = "生成期间出现订单、广告或成本硬缺口，经营暂结写入已拦截。"
                elif _text(live_after.get("report_hash")) != approved_report_hash:
                    block_reason = "生成期间报表内容已变化，经营暂结写入已拦截。"
                latest = await _get_status(period, tok) or {}
                latest_fields = latest.get("fields") or {}
                if _text(latest_fields.get("状态")) not in ("运营已确认", "财务已确认暂结"):
                    block_reason = "生成期间月结状态已变化，经营暂结写入已拦截。"
                if not block_reason and _ACTION_EPOCHS.get(period) != action_epoch:
                    block_reason = "生成期间收到新的月结操作，经营暂结写入已拦截。"

            if not block_reason:
                try:
                    async with db.ml_close_action_finalization_guard(
                        period,
                        action_key,
                        action_owner,
                        _text(report.get("content_hash")),
                    ) as generation_ready:
                        if not generation_ready:
                            block_reason = "经营暂结报表生成被新的退回或重算操作中断。"
                        else:
                            latest = await _get_status(period, tok) or {}
                            latest_fields = latest.get("fields") or {}
                            latest_result = _last_result(latest_fields)
                            due_on, due_ms = _final_reconciliation_due(month)
                            spreadsheet_token = _text(report.get("spreadsheet_token"))
                            sheet_url = (
                                f"https://u1wpma3xuhr.feishu.cn/sheets/{spreadsheet_token}"
                                if spreadsheet_token
                                else ""
                            )
                            latest_result.update({
                                "period": period,
                                "month": month,
                                "report_hash": approved_report_hash,
                                "operating_close_confirmed": True,
                                "operating_report_hash": approved_report_hash,
                                "operating_content_hash": _text(report.get("content_hash")),
                                "operating_report_url": _text(report.get("url")),
                                "operating_report_sheet_url": sheet_url,
                                "operating_closed_at": now_ms,
                                "operating_closed_by": actor,
                                "final_reconciliation_due_on": due_on,
                                "final_reconciliation_status": "待官方账单核销",
                            })
                            await _upsert_status(
                                period,
                                {
                                    "状态": "财务已确认暂结",
                                    "经营暂结确认人": actor,
                                    "经营暂结确认时间": now_ms,
                                    "经营暂结报表链接": report.get("url") or "",
                                    "报表链接": report.get("url") or "",
                                    "最终核销状态": "待官方账单核销",
                                    "最终核销截止日": due_ms,
                                    "最后错误": "",
                                    "最后按钮动作Key": action_key,
                                    "最后按钮动作时间": now_ms,
                                    "最后结果JSON": json.dumps(latest_result, ensure_ascii=False),
                                },
                                tok,
                            )
                except Exception as exc:
                    block_reason = f"经营暂结终态保护失败：{type(exc).__name__}"
        if block_reason:
            return await _blocked_confirmation(block_reason)
        processed = build_processed_card(
            month,
            "财务已确认暂结",
            actor,
            "经营暂结报表已冻结，可用于运营提成和公司管理毛利；官方账单仍待最终核销。",
            report_url=report.get("url") or "",
        )
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {
            "status": "ok",
            "action": action,
            "period": period,
            "state": "财务已确认暂结",
            "report": report,
            "processed_card": processed,
            "feedback": feedback,
        }

    if action == "ml_profit_finance_confirm":
        from app import unified_report

        block_reason = ""
        report: dict[str, Any] = {}

        async def _publish_retryable_finance_error(
            reason: str,
            required_report_hash: str | None = None,
        ) -> bool:
            async with db.ml_close_action_finalization_guard(
                period,
                action_key,
                action_owner,
                required_report_hash,
                complete_on_success=False,
            ) as latest_action:
                if not latest_action:
                    return False
                await _upsert_status(
                    period,
                    {
                        "状态": "异常",
                        "最后错误": reason[:1000],
                        "最后按钮动作Key": action_key,
                        "最后按钮动作时间": now_ms,
                    },
                    tok,
                )
                return True

        async with _status_mutation_lock(period):
            latest = await _get_status(period, tok) or {}
            latest_fields = latest.get("fields") or {}
            try:
                latest_failed = await _open_ad_failures(period, latest_fields)
            except Exception as e:
                latest_failed = []
                block_reason = f"广告失败状态读取失败：{type(e).__name__}"
            if latest_failed:
                block_reason = _ad_failure_message(latest_failed)
            latest_state = _text(latest_fields.get("状态"))
            retrying_generator_error = (
                latest_state == "异常"
                and _text(latest_fields.get("最后错误")).startswith(
                    ("月报生成失败：", "月报终态保护失败：")
                )
            )
            if (
                not block_reason
                and latest_state not in (
                    "运营已确认", "财务已确认终稿"
                )
                and not retrying_generator_error
            ):
                block_reason = f"当前月结状态为“{latest_state or '未知'}”，请先完成运营确认。"
            if not block_reason and _ACTION_EPOCHS.get(period) != action_epoch:
                block_reason = "生成前收到新的月结操作，本次财务确认已拦截。"
            if not block_reason:
                live_before = await audit(
                    period=period,
                    commit=False,
                    run_cost_preview=False,
                )
                if (
                    live_before.get("ab_verified") is not True
                    or _text(live_before.get("report_hash")) != approved_report_hash
                ):
                    block_reason = "生成前报表内容已变化，当前版本需重新完成 A/B 对账。"

            # 同一个月份从状态门禁、生成、回读到写终态都在同一把锁内，避免
            # “财务确认”和“退回重算”交叉覆盖。生成器自身另有跨进程持久占位。
            if not block_reason:
                try:
                    report = await unified_report.generate(
                        period,
                        commit=True,
                        expected_source_hash=approved_report_hash,
                    )
                except unified_report.ReportGenerationInProgressError as exc:
                    block_reason = f"月报生成失败：{exc}"
                    await _publish_retryable_finance_error(block_reason)
                except Exception as exc:
                    block_reason = f"月报生成失败：{exc}"
                    await _publish_retryable_finance_error(block_reason)

            if not block_reason:
                # 外部接口失败标记不依赖本进程锁，所以生成后再读一次，确保
                # 生成期间没有新出现的广告抓取失败。
                live_after = await audit(
                    period=period,
                    commit=False,
                    run_cost_preview=False,
                )
                if (
                    live_after.get("ab_verified") is not True
                    or _text(live_after.get("report_hash")) != approved_report_hash
                ):
                    block_reason = "生成期间报表内容已变化，终稿写入已拦截；请重新完成 A/B 对账。"
                latest = await _get_status(period, tok) or {}
                latest_fields = latest.get("fields") or {}
                try:
                    latest_failed = await _open_ad_failures(period, latest_fields)
                except Exception as e:
                    latest_failed = []
                    block_reason = f"广告失败状态读取失败：{type(e).__name__}"
                if latest_failed:
                    block_reason = _ad_failure_message(latest_failed)
                latest_state = _text(latest_fields.get("状态"))
                retrying_generator_error = (
                    latest_state == "异常"
                    and _text(latest_fields.get("最后错误")).startswith(
                        ("月报生成失败：", "月报终态保护失败：")
                    )
                )
                if (
                    not block_reason
                    and latest_state not in (
                        "运营已确认", "财务已确认终稿"
                    )
                    and not retrying_generator_error
                ):
                    block_reason = f"生成期间月结状态变为“{latest_state or '未知'}”，终稿写入已拦截。"
                if not block_reason and _ACTION_EPOCHS.get(period) != action_epoch:
                    block_reason = "生成期间收到新的月结操作，终稿写入已拦截。"
                if not block_reason:
                    try:
                        async with db.ml_close_action_finalization_guard(
                            period,
                            action_key,
                            action_owner,
                            _text(report.get("content_hash")),
                        ) as generation_ready:
                            if not generation_ready:
                                block_reason = "月报生成被新的退回/失败操作中断，终稿写入已拦截。"
                            else:
                                # SQLite write lock is deliberately held across this Feishu
                                # write. Reject/recalc/ad-failure must publish cancellation
                                # through the same DB first, so their state write is ordered
                                # after this one instead of being overwritten by it.
                                final_result = _last_result(latest_fields)
                                final_result.update({
                                    "period": period,
                                    "month": month,
                                    "report_hash": approved_report_hash,
                                    "ab_verified": True,
                                    "ab_report_hash": approved_report_hash,
                                    "final_reconciliation_status": "已完成",
                                    "final_report_url": _text(report.get("url")),
                                    "final_content_hash": _text(report.get("content_hash")),
                                    "final_closed_at": now_ms,
                                    "final_closed_by": actor,
                                })
                                await _upsert_status(
                                    period,
                                    {
                                        "状态": "财务已确认终稿",
                                        "财务确认人": actor,
                                        "财务确认时间": now_ms,
                                        "报表链接": report.get("url") or "",
                                        "最终核销状态": "已完成",
                                        "最后错误": "",
                                        "最后按钮动作Key": action_key,
                                        "最后按钮动作时间": now_ms,
                                        "最后结果JSON": json.dumps(final_result, ensure_ascii=False),
                                    },
                                    tok,
                                )
                    except Exception as e:
                        block_reason = f"月报终态保护失败：{type(e).__name__}"
                        await _publish_retryable_finance_error(
                            block_reason,
                            _text(report.get("content_hash")),
                        )
        if block_reason:
            return await _blocked_confirmation(block_reason)
        processed = build_processed_card(
            month,
            "财务已确认终稿",
            actor,
            "统一格式月报已生成并通过写后回读。",
            report_url=report.get("url") or "",
        )
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {
            "status": "ok",
            "action": action,
            "period": period,
            "state": "财务已确认终稿",
            "report": report,
            "processed_card": processed,
            "feedback": feedback,
        }

    if action == "ml_profit_finance_reject":
        async with _status_mutation_lock(period):
            latest = await _get_status(period, tok) or {}
            latest_fields = latest.get("fields") or {}
            if _text(latest_fields.get("状态")) == "财务已确认终稿":
                block_reason = "本月已确认终稿，旧财务退回卡片已失效。"
            if not block_reason and _ACTION_EPOCHS.get(period) != action_epoch:
                block_reason = "退回期间收到新的月结操作，本次财务退回已拦截。"
            if not block_reason:
                async with db.ml_close_action_finalization_guard(
                    period, action_key, action_owner
                ) as latest_action:
                    if not latest_action:
                        block_reason = "退回期间收到新的月结操作，本次财务退回已拦截。"
                    else:
                        guard_status = await _get_status(period, tok) or {}
                        guard_fields = guard_status.get("fields") or {}
                        if _text(guard_fields.get("状态")) == "财务已确认终稿":
                            block_reason = "本月已确认终稿，旧财务退回卡片已失效。"
                        else:
                            await _upsert_status(
                                period,
                                {
                                    "状态": "退回重算",
                                    "最后错误": "财务退回运营复核",
                                    "最后按钮动作Key": action_key,
                                    "最后按钮动作时间": now_ms,
                                },
                                tok,
                            )
        if block_reason:
            return await _blocked_confirmation(block_reason)
        processed = build_processed_card(month, "退回运营复核", actor, "财务退回，需运营复核后再确认", ok=False)
        feedback = await patch_or_fallback(message_id, processed, chat_id) if patch else {}
        return {"status": "ok", "action": action, "period": period, "state": "退回重算", "processed_card": processed, "feedback": feedback}

    return {"status": "ignored", "action": action, "period": period, "msg": "unknown ml close action"}
