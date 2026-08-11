# -*- coding: utf-8 -*-
"""财务统一格式的美客多毛利月报生成器。

业务边界：采购成本沿用月度生产表按 ERP SKU 已计算的结果；本模块只用
ERP SKU 为报表补充 ERP品名和类目，不用名称或类目反向匹配成本。
"""
from __future__ import annotations

import datetime as dt
import asyncio
import hashlib
import json
import os
import re
import unicodedata
import uuid
import weakref
from collections import defaultdict
from typing import Any
from urllib.parse import quote

import httpx

from app import db


FEISHU = "https://open.feishu.cn/open-apis"
REPORT_APP_TOKEN = os.getenv("FEISHU_BASE_APP_TOKEN", "WM3LbBr76aRqMys2of8c1dGInEb")
REPORT_TABLE_ID = os.getenv("FEISHU_BASE_TABLE_ID", "tbl09sRPkX35PDfU")
PRODUCT_APP_TOKEN = os.getenv("ML_PRODUCT_BASE_APP_TOKEN", "MvtZb6OE9aJFaisO913cWSErnFe")
PRODUCT_COST_TABLE_ID = os.getenv("ML_PRODUCT_COST_TABLE_ID", "tblyxyp9BQBkIOID")
PRODUCT_MAINTENANCE_TABLE_ID = os.getenv("ML_PRODUCT_MAINTENANCE_TABLE_ID", "tblTvqipcTBFRUkr")
TEMPLATE_WIKI_TOKEN = os.getenv("ML_UNIFIED_REPORT_TEMPLATE_WIKI_TOKEN", "Nkmxw5a07iyh29kXe9DcwH2gnZd")
PARENT_WIKI_TOKEN = os.getenv("ML_UNIFIED_REPORT_PARENT_WIKI_TOKEN", "BvsgwyjNtiTqxXkiB3actiOqnFe")

REPORT_HEADERS = [
    "Listing负责人", "国家", "店铺", "月份", "MSKU", "中文名称", "分类", "Listing标签", "币种", "销量",
    "退货数量", "销售额(原币)", "退货(原币)", "佣金(原币)", "配送费(原币)", "仓储费(原币)", "广告费(原币)",
    "调整(原币)", "推广费(原币)", "VAT预缴（原币）", "回款(原币)", "采购成本（原币）", "头程成本（原币）",
    "毛利润（原币）", "我的汇率", "售价(RMB)", "退货(RMB)", "佣金(RMB)", "配送费(RMB)", "仓储费(RMB)",
    "广告费(RMB)", "调整(RMB)", "推广费(RMB)", "VAT预缴（RMB）", "回款(RMB)", "采购成本（RMB)",
    "头程费用（RMB)", "毛利润（RMB)", "退货率", "佣金占比", "配送费占比", "仓储费占比", "推广费占比",
    "采购成本占比", "头程运费占比", "毛利率", "回款率",
]

# 与财务验收的 2026-07「数据源」sheet 顺序一致；最后另加 record_id。
SOURCE_HEADERS = [
    "SKU", "全额毛利(RMB)", "ML佣金(RMB)", "商品标题", "TACOS", "首次销售日", "整店访客", "父记录",
    "退款金额(原币)", "订单数", "店铺", "Full仓储费(RMB)", "卖家折扣(原币)", "营收(原币)", "我的汇率",
    "广告CVR", "采购成本(RMB)", "自然销售占比", "CTR", "件数", "ML佣金(原币)", "广告费(原币)",
    "客单价(原币)", "整店CVR", "海外仓成本(RMB)", "广告展示", "周期", "物流费(RMB)", "营收(RMB)",
    "数据拉取时间", "VAT估算(RMB)", "广告直接销售(原币)", "CPC(原币)", "广告费(RMB)", "广告归因件数",
    "卖家折扣(RMB)", "广告点击", "平台", "币种", "退款率", "ML ROAS", "简易毛利(RMB)", "VAT估算(原币)",
    "物流费(原币)", "退款金额(RMB)", "自然销售(RMB)", "头程成本(RMB)", "最后销售日",
]

STORE_ORDER = [
    "ML 巴西本土店 AIRSOFT COMERCIAL",
    "ML CBT-FULL (1502236229)",
    "ML 本土3店 DISTRIBUIDOR VALMIGOZ",
]

MARKER_PREFIX = "ML_UNIFIED_REPORT_V1"
COMMISSION_TOLERANCE_RMB = 0.05
PROFIT_TOLERANCE_RMB = 0.02

_GENERATION_LOCKS_BY_LOOP: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()


class ProductMappingError(RuntimeError):
    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        skus = "、".join(str(issue.get("sku") or "(空SKU)") for issue in issues[:10])
        more = f"，另有 {len(issues) - 10} 个" if len(issues) > 10 else ""
        super().__init__(f"ERP品名/类目映射失败：{skus}{more}")


class ReportGenerationError(RuntimeError):
    pass


class ReportGenerationInProgressError(ReportGenerationError):
    pass


def _generation_lock(period: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _GENERATION_LOCKS_BY_LOOP.setdefault(loop, {})
    return locks.setdefault(period, asyncio.Lock())


def normalize_sku(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).strip()
    text = re.sub(r"[\s_]+", "", text)
    text = re.sub(r"[‐‑‒–—―]", "-", text)
    return text.upper()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " / ".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "").strip()
    return str(value).strip()


def _number(value: Any) -> float:
    if isinstance(value, list):
        value = value[0] if value else 0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else record


def _unique_text(records: list[dict[str, Any]], field: str) -> list[str]:
    return sorted({_text(_record_fields(record).get(field)) for record in records if _text(_record_fields(record).get(field))})


def _mapping_tiers(
    maintenance_records: list[dict[str, Any]],
    cost_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "source": "产品信息维护表.ERP SKU",
            "records": maintenance_records,
            "match_field": "ERP SKU",
            "category_fields": ["产品类型", "三级分类"],
        },
        {
            "source": "产品采购成本台.ERP SKU",
            "records": cost_records,
            "match_field": "ERP SKU",
            "category_fields": ["三级分类", "二级分类"],
        },
        {
            "source": "产品采购成本台.分销报价单SKU（Model No. ）",
            "records": cost_records,
            "match_field": "分销报价单SKU（Model No. ）",
            "category_fields": ["三级分类", "二级分类"],
        },
    ]


def resolve_product_mappings(
    skus: list[str],
    maintenance_records: list[dict[str, Any]],
    cost_records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve ERP display fields with strict tier precedence and fail closed."""
    tiers = _mapping_tiers(maintenance_records, cost_records)
    indexes: list[dict[str, list[dict[str, Any]]]] = []
    for tier in tiers:
        index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in tier["records"]:
            key = normalize_sku(_record_fields(record).get(tier["match_field"]))
            if key:
                index[key].append(record)
        indexes.append(index)

    resolved: dict[str, dict[str, Any]] = {}
    resolved_by_normalized: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_sku in skus:
        normalized = normalize_sku(raw_sku)
        if normalized in seen:
            if normalized in resolved_by_normalized:
                resolved[raw_sku] = resolved_by_normalized[normalized]
            continue
        seen.add(normalized)
        if not normalized:
            issues.append({"sku": raw_sku, "reason": "blank_sku", "source": "月度生产表"})
            continue
        match = None
        tier = None
        for candidate_tier, index in zip(tiers, indexes):
            records = index.get(normalized, [])
            if records:
                match = records
                tier = candidate_tier
                break
        if not match or not tier:
            issues.append({"sku": raw_sku, "reason": "unmatched", "source": "ERP产品库"})
            continue

        names = _unique_text(match, "ERP品名")
        if not names:
            issues.append({"sku": raw_sku, "reason": "missing_name", "source": tier["source"]})
            continue
        if len(names) > 1:
            issues.append({
                "sku": raw_sku,
                "reason": "conflicting_name",
                "source": tier["source"],
                "values": names,
            })
            continue

        category = ""
        category_field = ""
        category_conflict: list[str] = []
        for field in tier["category_fields"]:
            values = _unique_text(match, field)
            if not values:
                continue
            category_field = field
            if len(values) > 1:
                category_conflict = values
            else:
                category = values[0]
            break
        if category_conflict:
            issues.append({
                "sku": raw_sku,
                "reason": "conflicting_category",
                "source": tier["source"],
                "field": category_field,
                "values": category_conflict,
            })
            continue
        if not category:
            issues.append({"sku": raw_sku, "reason": "missing_category", "source": tier["source"]})
            continue

        mapped = {
            "normalized_sku": normalized,
            "product_name": names[0],
            "category": category,
            "category_field": category_field,
            "source": tier["source"],
            "record_ids": [record.get("record_id") for record in match if record.get("record_id")],
        }
        resolved[raw_sku] = mapped
        resolved_by_normalized[normalized] = mapped

    if issues:
        raise ProductMappingError(issues)
    return resolved


def _formula(text: str) -> dict[str, str]:
    # Feishu Sheets' values API represents a formula as this typed cell object.
    return {"type": "formula", "text": text}


def _column_letter(number: int) -> str:
    output = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _sheet_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _text(value)


def _sheet_cell_matches(actual: Any, expected: Any) -> bool:
    if expected is None or expected == "":
        return _text(actual) == ""
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(actual) - float(expected)) <= 1e-6
        except (TypeError, ValueError):
            return False
    return _text(actual) == _text(expected)


def _sort_key(record: dict[str, Any]) -> tuple[int, str]:
    fields = _record_fields(record)
    store = _text(fields.get("店铺"))
    try:
        store_index = STORE_ORDER.index(store)
    except ValueError:
        store_index = len(STORE_ORDER)
    return store_index, normalize_sku(fields.get("SKU"))


def _period_parts(period: str) -> tuple[str, str]:
    clean = (period or "").strip()
    month = clean.removeprefix("month_")
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise ValueError(f"invalid month/period: {period}")
    return f"month_{month}", month


def prepare_report(
    period: str,
    report_records: list[dict[str, Any]],
    maintenance_records: list[dict[str, Any]],
    cost_records: list[dict[str, Any]],
) -> dict[str, Any]:
    period, month = _period_parts(period)
    rows = [
        record for record in report_records
        if _text(_record_fields(record).get("周期")) == period
    ]
    rows.sort(key=_sort_key)
    if not rows:
        raise ReportGenerationError(f"{month} 没有可生成的美客多月报数据")

    skus = [_text(_record_fields(row).get("SKU")) for row in rows]
    mappings = resolve_product_mappings(skus, maintenance_records, cost_records)

    source_headers = SOURCE_HEADERS + ["record_id"]
    source_values: list[list[Any]] = [source_headers]
    for record in rows:
        fields = _record_fields(record)
        source_values.append(
            [_sheet_scalar(fields.get(name)) for name in SOURCE_HEADERS] + [record.get("record_id") or ""]
        )
    source_columns = {name: _column_letter(index + 1) for index, name in enumerate(source_headers)}

    main_values: list[list[Any]] = [REPORT_HEADERS]
    for index, record in enumerate(rows, start=2):
        fields = _record_fields(record)
        sku = _text(fields.get("SKU"))
        mapped = mappings[sku]
        row: list[Any] = [None] * len(REPORT_HEADERS)
        row[0] = "梁俊辉"
        row[3] = month
        row[5] = mapped["product_name"]
        row[6] = mapped["category"]
        row[7] = ""
        row[10] = None
        row[17] = 0
        row[18] = 0
        row[31] = 0
        row[32] = 0

        def ref(field: str) -> str:
            return f"'数据源'!{source_columns[field]}{index}"

        row[1] = _formula(f'=IF({ref("币种")}="BRL","巴西","墨西哥")')
        row[2] = _formula(f'={ref("店铺")}')
        row[4] = _formula(f'={ref("SKU")}')
        row[8] = _formula(f'={ref("币种")}')
        row[9] = _formula(f'={ref("件数")}')
        row[11] = _formula(f'={ref("营收(原币)")}')
        row[12] = _formula(f'=-{ref("退款金额(原币)")}')
        row[13] = _formula(f'=-{ref("ML佣金(原币)")}')
        row[14] = _formula(f'=-{ref("物流费(原币)")}')
        row[15] = _formula(f'=ROUND(IFERROR(-{ref("Full仓储费(RMB)")}/{ref("我的汇率")},0),2)')
        row[16] = _formula(f'=-{ref("广告费(原币)")}')
        row[19] = _formula(f'=-{ref("VAT估算(原币)")}')
        row[20] = _formula(f'=ROUND(SUM(L{index}:T{index}),2)')
        row[21] = _formula(f'=ROUND(IFERROR(-{ref("采购成本(RMB)")}/{ref("我的汇率")},0),2)')
        row[22] = _formula(
            f'=ROUND(IFERROR(-({ref("头程成本(RMB)")}+{ref("海外仓成本(RMB)")})/{ref("我的汇率")},0),2)'
        )
        row[23] = _formula(f'=ROUND(SUM(U{index}:W{index}),2)')
        row[24] = _formula(f'={ref("我的汇率")}')
        row[25] = _formula(f'=ROUND(L{index}*Y{index},2)')
        row[26] = _formula(f'=ROUND(M{index}*Y{index},2)')
        row[27] = _formula(f'=ROUND(N{index}*Y{index},2)')
        row[28] = _formula(f'=ROUND(O{index}*Y{index},2)')
        row[29] = _formula(f'=ROUND(P{index}*Y{index},2)')
        row[30] = _formula(f'=ROUND(Q{index}*Y{index},2)')
        row[33] = _formula(f'=ROUND(T{index}*Y{index},2)')
        row[34] = _formula(f'=ROUND(SUM(Z{index}:AH{index}),2)')
        row[35] = _formula(f'=ROUND(V{index}*Y{index},2)')
        row[36] = _formula(f'=ROUND(W{index}*Y{index},2)')
        row[37] = _formula(f'=ROUND(SUM(AI{index}:AK{index}),2)')
        row[38] = _formula(f'=IFERROR(-AA{index}/Z{index},0)')
        row[39] = _formula(f'=IFERROR(AB{index}/Z{index},0)')
        row[40] = _formula(f'=IFERROR(AC{index}/Z{index},0)')
        row[41] = _formula(f'=IFERROR(AD{index}/Z{index},0)')
        row[42] = _formula(f'=IFERROR((AE{index}+AG{index})/Z{index},0)')
        row[43] = _formula(f'=IFERROR(AJ{index}/Z{index},0)')
        row[44] = _formula(f'=IFERROR(AK{index}/Z{index},0)')
        row[45] = _formula(f'=IFERROR(AL{index}/Z{index},0)')
        row[46] = _formula(f'=IFERROR(AI{index}/Z{index},0)')
        main_values.append(row)

    def sum_field(name: str) -> float:
        return sum(_number(_record_fields(record).get(name)) for record in rows)

    commission_deltas = []
    profit_deltas = []
    for record in rows:
        fields = _record_fields(record)
        commission_deltas.append(abs(
            _number(fields.get("ML佣金(RMB)"))
            - _number(fields.get("ML佣金(原币)")) * _number(fields.get("我的汇率"))
        ))
        calculated_profit = (
            _number(fields.get("营收(RMB)"))
            - _number(fields.get("采购成本(RMB)"))
            - _number(fields.get("ML佣金(RMB)"))
            - _number(fields.get("广告费(RMB)"))
            - _number(fields.get("VAT估算(RMB)"))
            - _number(fields.get("物流费(RMB)"))
            - _number(fields.get("退款金额(RMB)"))
            - _number(fields.get("Full仓储费(RMB)"))
            - _number(fields.get("头程成本(RMB)"))
            - _number(fields.get("海外仓成本(RMB)"))
        )
        profit_deltas.append(abs(_number(fields.get("全额毛利(RMB)")) - calculated_profit))
    max_commission_delta = max(commission_deltas, default=0)
    max_profit_delta = max(profit_deltas, default=0)

    mapping_sources: dict[str, int] = defaultdict(int)
    for mapped in mappings.values():
        mapping_sources[mapped["source"]] += 1
    summary = {
        "period": period,
        "month": month,
        "report_rows": len(rows),
        "unique_skus": len({normalize_sku(sku) for sku in skus}),
        "store_count": len({_text(_record_fields(row).get("店铺")) for row in rows}),
        "orders": int(round(sum_field("订单数"))),
        "units": sum_field("件数"),
        "revenue_rmb": round(sum_field("营收(RMB)"), 2),
        "commission_rmb": round(sum_field("ML佣金(RMB)"), 2),
        "advertising_rmb": round(sum_field("广告费(RMB)"), 2),
        "full_profit_rmb": round(sum_field("全额毛利(RMB)"), 2),
        "mapping_sources": dict(mapping_sources),
        "mapping_issues": 0,
        "commission_max_abs_delta": round(max_commission_delta, 6),
        "commission_check": "通过" if max_commission_delta <= COMMISSION_TOLERANCE_RMB else "需复核",
        "profit_max_abs_delta": round(max_profit_delta, 6),
        "profit_check": "通过" if max_profit_delta <= PROFIT_TOLERANCE_RMB else "需复核",
    }
    check_values = [
        ["检查项目", "结果", "判断标准 / 说明", "", "生成器标记"],
        ["数据期间", month, f"生产表周期 {period}", "", ""],
        ["记录数", len(rows), "SKU×店铺明细", "", ""],
        ["ERP映射", "通过", f"{summary['unique_skus']} 个唯一 SKU，未命中/冲突/空值均为 0", "", ""],
        ["店铺数", summary["store_count"], "生产表汇总", "", ""],
        ["订单数", summary["orders"], "生产表汇总", "", ""],
        ["销量", summary["units"], "生产表汇总", "", ""],
        ["营收(RMB)", summary["revenue_rmb"], "生产表汇总", "", ""],
        ["佣金(RMB)", summary["commission_rmb"], "生产表汇总", "", ""],
        ["广告费(RMB)", summary["advertising_rmb"], "生产表汇总", "", ""],
        ["全额毛利(RMB)", summary["full_profit_rmb"], "生产表汇总", "", ""],
        ["佣金换算", summary["commission_check"], f"佣金原币×精确汇率；最大差额 {max_commission_delta:.4f} 元", "", ""],
        ["汇率显示", "4位小数", "避免截图中的显示精度造成佣金换算误判", "", ""],
        ["毛利公式复核", summary["profit_check"], f"生产表逐行重算；最大差额 {max_profit_delta:.4f} 元", "", ""],
        ["退货数量", "待补", "生产表无退货件数字段；退货率暂按退款金额÷营收", "", ""],
        ["产品中文名/分类", "通过", "ERP SKU精确映射；仅用于展示，不作为采购成本关联键", "", ""],
        ["生成时间", dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"), "北京时间", "", ""],
        ["数据来源", f"{REPORT_APP_TOKEN}/{REPORT_TABLE_ID}", "飞书生产表，只读拉取", "", ""],
    ]
    return {
        "summary": summary,
        "main_values": main_values,
        "source_values": source_values,
        "check_values": check_values,
    }


def report_content_hash(prepared: dict[str, Any]) -> str:
    """Hash business inputs and ERP display mapping, excluding generation time."""
    payload = {
        "main_values": prepared["main_values"],
        "source_values": prepared["source_values"],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


async def _tenant_token(app_id: str, secret: str) -> str:
    if not secret:
        raise ReportGenerationError(f"Feishu secret missing for app {app_id}")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{FEISHU}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": secret},
        )
    payload = response.json()
    token = payload.get("tenant_access_token")
    if response.status_code >= 400 or not token:
        raise ReportGenerationError(f"Feishu tenant token failed for {app_id}: {payload}")
    return token


async def _api_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, headers=headers, json=payload, params=params)
    try:
        body = response.json()
    except Exception as exc:
        raise ReportGenerationError(f"Feishu non-json response {response.status_code}: {response.text[:500]}") from exc
    if response.status_code >= 400 or body.get("code") not in (0, None):
        raise ReportGenerationError(f"Feishu API failed {method} {url}: {body}")
    return body


async def _list_bitable_records(token: str, app_token: str, table_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        body = await _api_json(
            "GET",
            f"{FEISHU}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            token,
            params=params,
        )
        data = body.get("data") or {}
        records.extend(data.get("items") or [])
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return records


async def _load_sources() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    report_app_id = os.getenv("FEISHU_APP_ID", "cli_a9f6ae86fce8dbd8")
    report_secret = os.getenv("FEISHU_APP_SECRET", "")
    dedicated_product_app_id = os.getenv("ML_PRODUCT_FEISHU_APP_ID")
    dedicated_product_secret = os.getenv("ML_PRODUCT_FEISHU_APP_SECRET")
    legacy_product_app_id = os.getenv("FEISHU_BITABLE_APP_ID")
    legacy_product_secret = os.getenv("FEISHU_BITABLE_APP_SECRET")
    if dedicated_product_app_id or dedicated_product_secret:
        product_app_id = dedicated_product_app_id or "cli_a93785277ef8dcb0"
        product_secret = dedicated_product_secret or ""
    elif legacy_product_app_id or legacy_product_secret:
        product_app_id = legacy_product_app_id or "cli_a93785277ef8dcb0"
        product_secret = legacy_product_secret or ""
    else:
        product_app_id = report_app_id
        product_secret = report_secret
    report_token = await _tenant_token(report_app_id, report_secret)
    product_token = await _tenant_token(product_app_id, product_secret)
    report_records = await _list_bitable_records(report_token, REPORT_APP_TOKEN, REPORT_TABLE_ID)
    maintenance_records = await _list_bitable_records(
        product_token, PRODUCT_APP_TOKEN, PRODUCT_MAINTENANCE_TABLE_ID
    )
    cost_records = await _list_bitable_records(product_token, PRODUCT_APP_TOKEN, PRODUCT_COST_TABLE_ID)
    return report_records, maintenance_records, cost_records


async def _report_token() -> str:
    return await _tenant_token(
        os.getenv("FEISHU_APP_ID", "cli_a9f6ae86fce8dbd8"),
        os.getenv("FEISHU_APP_SECRET", ""),
    )


async def _spreadsheet_meta(token: str, spreadsheet_token: str) -> list[dict[str, Any]]:
    body = await _api_json(
        "GET", f"{FEISHU}/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo", token
    )
    return body.get("data", {}).get("sheets") or []


def _sheet_by_title(sheets: list[dict[str, Any]], title: str) -> dict[str, Any]:
    for sheet in sheets:
        if sheet.get("title") == title:
            return sheet
    raise ReportGenerationError(f"模板缺少工作表：{title}")


def _main_sheet(sheets: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [sheet for sheet in sheets if sheet.get("title") not in ("数据源", "检查")]
    if len(candidates) != 1:
        raise ReportGenerationError("模板主表数量异常，无法确定 47 列毛利主表")
    return candidates[0]


async def _read_range(
    token: str,
    spreadsheet_token: str,
    cell_range: str,
    value_render_option: str = "FormattedValue",
) -> list[list[Any]]:
    encoded = quote(cell_range, safe="!:")
    body = await _api_json(
        "GET",
        f"{FEISHU}/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded}",
        token,
        params={"valueRenderOption": value_render_option},
    )
    return body.get("data", {}).get("valueRange", {}).get("values") or []


async def _write_range(
    token: str,
    spreadsheet_token: str,
    cell_range: str,
    values: list[list[Any]],
) -> None:
    await _api_json(
        "PUT",
        f"{FEISHU}/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        token,
        {"valueRange": {"range": cell_range, "values": values}},
        timeout=120,
    )


async def _wiki_space_id(token: str) -> str:
    body = await _api_json(
        "GET", f"{FEISHU}/wiki/v2/spaces/get_node", token, params={"token": PARENT_WIKI_TOKEN}
    )
    node = body.get("data", {}).get("node") or {}
    space_id = node.get("space_id")
    if not space_id:
        raise ReportGenerationError("无法读取财务报表汇总知识库空间")
    return space_id


async def _wiki_children(token: str, space_id: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"parent_node_token": PARENT_WIKI_TOKEN, "page_size": 50}
        if page_token:
            params["page_token"] = page_token
        body = await _api_json(
            "GET", f"{FEISHU}/wiki/v2/spaces/{space_id}/nodes", token, params=params
        )
        data = body.get("data") or {}
        nodes.extend(data.get("items") or [])
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return nodes


async def _find_existing_report(
    token: str, period: str, month: str
) -> dict[str, Any] | None:
    space_id = await _wiki_space_id(token)
    title = f"美客多毛利报表-{month}"
    matches = [node for node in await _wiki_children(token, space_id) if node.get("title") == title]
    if len(matches) > 1:
        raise ReportGenerationError(f"知识库中存在多个同名报表：{title}")
    if matches:
        node = matches[0]
        if node.get("obj_type") != "sheet" or not node.get("obj_token"):
            raise ReportGenerationError(f"同名知识库节点不是电子表格：{title}")
        spreadsheet_token = node["obj_token"]
        sheets = await _spreadsheet_meta(token, spreadsheet_token)
        check_sheet = _sheet_by_title(sheets, "检查")
        marker_rows = await _read_range(
            token, spreadsheet_token, f"{check_sheet['sheetId']}!E1:E1", "FormattedValue"
        )
        marker = _text(marker_rows[0][0]) if marker_rows and marker_rows[0] else ""
        base = {
            "wiki_token": node.get("node_token"),
            "spreadsheet_token": spreadsheet_token,
            "url": f"https://u1wpma3xuhr.feishu.cn/wiki/{node.get('node_token')}",
            "sheets": sheets,
        }
        if marker.startswith(f"{MARKER_PREFIX}|COMPLETE|{period}|"):
            return {**base, "complete": True, "content_hash": marker.rsplit("|", 1)[-1]}
        if marker == f"{MARKER_PREFIX}|IN_PROGRESS|{period}":
            return {**base, "complete": False}
        raise ReportGenerationError(f"同名报表已存在但不是本生成器产物，未覆盖：{title}")
    return None


async def _copy_or_resume_report(token: str, period: str, month: str) -> dict[str, Any]:
    existing = await _find_existing_report(token, period, month)
    if existing:
        return existing

    space_id = await _wiki_space_id(token)
    title = f"美客多毛利报表-{month}"

    body = await _api_json(
        "POST",
        f"{FEISHU}/wiki/v2/spaces/{space_id}/nodes/{TEMPLATE_WIKI_TOKEN}/copy",
        token,
        {"target_parent_token": PARENT_WIKI_TOKEN, "target_space_id": space_id, "title": title},
        timeout=120,
    )
    node = body.get("data", {}).get("node") or {}
    spreadsheet_token = node.get("obj_token")
    wiki_token = node.get("node_token")
    if node.get("obj_type") != "sheet" or not spreadsheet_token or not wiki_token:
        raise ReportGenerationError(f"模板复制响应缺少电子表格信息：{body}")
    sheets = await _spreadsheet_meta(token, spreadsheet_token)
    check_sheet = _sheet_by_title(sheets, "检查")
    await _write_range(
        token,
        spreadsheet_token,
        f"{check_sheet['sheetId']}!E1:E1",
        [[f"{MARKER_PREFIX}|IN_PROGRESS|{period}"]],
    )
    return {
        "complete": False,
        "wiki_token": wiki_token,
        "spreadsheet_token": spreadsheet_token,
        "url": f"https://u1wpma3xuhr.feishu.cn/wiki/{wiki_token}",
        "sheets": sheets,
    }


async def _ensure_rows(
    token: str,
    spreadsheet_token: str,
    sheet: dict[str, Any],
    needed_rows: int,
) -> int:
    current = int(sheet.get("rowCount") or sheet.get("row_count") or 0)
    if current >= needed_rows:
        return current
    await _api_json(
        "POST",
        f"{FEISHU}/sheets/v2/spreadsheets/{spreadsheet_token}/insert_dimension_range",
        token,
        {
            "dimension": {
                "sheetId": sheet["sheetId"],
                "majorDimension": "ROWS",
                "startIndex": current,
                "endIndex": needed_rows,
            },
            "inheritStyle": "BEFORE",
        },
    )
    return needed_rows


def _pad_matrix(values: list[list[Any]], rows: int, columns: int) -> list[list[Any]]:
    if len(values) > rows:
        raise ReportGenerationError(f"写入行数 {len(values)} 超过工作表行数 {rows}")
    padded = [list(row[:columns]) + [None] * max(0, columns - len(row)) for row in values]
    padded.extend([[None] * columns for _ in range(rows - len(padded))])
    return padded


async def _style_report(
    token: str,
    spreadsheet_token: str,
    main_id: str,
    source_id: str,
    check_id: str,
    last_row: int,
) -> None:
    styles = [
        {"ranges": f"{main_id}!A1:AU1", "style": {"bold": True, "fontSize": 10, "hAlign": 1, "vAlign": 1, "foreColor": "#FFFFFF", "backColor": "#0F766E"}},
        {"ranges": f"{main_id}!A2:AU{last_row}", "style": {"fontSize": 9, "vAlign": 1}},
        {"ranges": f"{main_id}!L2:X{last_row}", "style": {"formatter": "#,##0.00;[Red]-#,##0.00"}},
        {"ranges": f"{main_id}!Z2:AL{last_row}", "style": {"formatter": "#,##0.00;[Red]-#,##0.00"}},
        {"ranges": f"{main_id}!Y2:Y{last_row}", "style": {"formatter": "0.0000"}},
        {"ranges": f"{main_id}!AM2:AU{last_row}", "style": {"formatter": "0.00%"}},
        {"ranges": f"{main_id}!J2:K{last_row}", "style": {"formatter": "0"}},
        {"ranges": f"{source_id}!A1:AW1", "style": {"bold": True, "fontSize": 10, "hAlign": 1, "vAlign": 1, "foreColor": "#FFFFFF", "backColor": "#0F766E"}},
        {"ranges": f"{source_id}!A2:AW{last_row}", "style": {"fontSize": 9, "vAlign": 1}},
        {"ranges": f"{check_id}!A1:C1", "style": {"bold": True, "fontSize": 10, "hAlign": 1, "vAlign": 1, "backColor": "#D9EAF7"}},
        {"ranges": f"{check_id}!A2:C18", "style": {"fontSize": 10, "vAlign": 1}},
        {"ranges": f"{check_id}!B8:B11", "style": {"formatter": "#,##0.00;[Red]-#,##0.00"}},
    ]
    await _api_json(
        "PUT",
        f"{FEISHU}/sheets/v2/spreadsheets/{spreadsheet_token}/styles_batch_update",
        token,
        {"data": styles},
    )


async def _write_report(
    token: str,
    report: dict[str, Any],
    prepared: dict[str, Any],
) -> dict[str, Any]:
    spreadsheet_token = report["spreadsheet_token"]
    sheets = report.get("sheets") or await _spreadsheet_meta(token, spreadsheet_token)
    main = _main_sheet(sheets)
    source = _sheet_by_title(sheets, "数据源")
    checks = _sheet_by_title(sheets, "检查")
    target_title = f"美客多毛利报表-{prepared['summary']['month']}"
    if main.get("title") != target_title:
        await _api_json(
            "POST",
            f"{FEISHU}/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update",
            token,
            {"requests": [{"updateSheet": {"properties": {"sheetId": main["sheetId"], "title": target_title}}}]},
        )

    needed = max(200, len(prepared["main_values"]), len(prepared["source_values"]), len(prepared["check_values"]))
    main_rows = await _ensure_rows(token, spreadsheet_token, main, needed)
    source_rows = await _ensure_rows(token, spreadsheet_token, source, needed)
    check_rows = await _ensure_rows(token, spreadsheet_token, checks, needed)

    check_values = [list(row) for row in prepared["check_values"]]
    check_values[0][4] = f"{MARKER_PREFIX}|IN_PROGRESS|{prepared['summary']['period']}"
    await _write_range(
        token, spreadsheet_token, f"{source['sheetId']}!A1:AW{source_rows}",
        _pad_matrix(prepared["source_values"], source_rows, 49),
    )
    await _write_range(
        token, spreadsheet_token, f"{main['sheetId']}!A1:AU{main_rows}",
        _pad_matrix(prepared["main_values"], main_rows, 47),
    )
    await _write_range(
        token, spreadsheet_token, f"{checks['sheetId']}!A1:T{check_rows}",
        _pad_matrix(check_values, check_rows, 20),
    )
    await _style_report(
        token, spreadsheet_token, main["sheetId"], source["sheetId"], checks["sheetId"],
        len(prepared["main_values"]),
    )

    main_last_row = len(prepared["main_values"])
    source_last_row = len(prepared["source_values"])
    check_last_row = len(check_values)
    main_read = await _read_range(
        token, spreadsheet_token, f"{main['sheetId']}!A1:AU{main_last_row}", "Formula"
    )
    source_read = await _read_range(
        token, spreadsheet_token, f"{source['sheetId']}!A1:AW{source_last_row}", "Formula"
    )
    checks_read = await _read_range(
        token, spreadsheet_token, f"{checks['sheetId']}!A1:T{check_last_row}", "Formula"
    )
    validate_report_readback(
        main_read,
        prepared["main_values"],
        source_read,
        prepared["source_values"],
        checks_read,
        check_values,
    )

    content_hash = report_content_hash(prepared)
    marker = f"{MARKER_PREFIX}|COMPLETE|{prepared['summary']['period']}|{content_hash}"
    await _write_range(token, spreadsheet_token, f"{checks['sheetId']}!E1:E1", [[marker]])
    marker_read = await _read_range(
        token, spreadsheet_token, f"{checks['sheetId']}!E1:E1", "FormattedValue"
    )
    actual_marker = _text(marker_read[0][0]) if marker_read and marker_read[0] else ""
    if actual_marker != marker:
        raise ReportGenerationError("统一毛利报表写后回读失败：完成标记不一致")
    return {"marker": marker, "content_hash": content_hash}


def validate_report_readback(
    actual_rows: list[list[Any]],
    expected_rows: list[list[Any]],
    actual_source_rows: list[list[Any]],
    expected_source_rows: list[list[Any]],
    actual_check_rows: list[list[Any]],
    expected_check_rows: list[list[Any]],
) -> None:
    """Fail closed unless headers, row count, ERP display and every formula survived."""
    if len(actual_rows) != len(expected_rows) or not actual_rows or actual_rows[0][:47] != REPORT_HEADERS:
        raise ReportGenerationError("统一毛利报表写后回读失败：标题或行数不一致")
    if any(len(row) < 7 or not _text(row[5]) or not _text(row[6]) for row in actual_rows[1:]):
        raise ReportGenerationError("统一毛利报表写后回读失败：ERP品名或分类为空")

    formula_mismatches: list[str] = []
    value_mismatches: list[str] = []
    for row_number, (actual, expected) in enumerate(zip(actual_rows[1:], expected_rows[1:]), start=2):
        for column_index, expected_cell in enumerate(expected):
            actual_cell = actual[column_index] if column_index < len(actual) else None
            if isinstance(expected_cell, dict) and expected_cell.get("type") == "formula":
                if _text(actual_cell) != _text(expected_cell.get("text")):
                    formula_mismatches.append(f"{_column_letter(column_index + 1)}{row_number}")
                continue
            if not _sheet_cell_matches(actual_cell, expected_cell):
                value_mismatches.append(f"{_column_letter(column_index + 1)}{row_number}")
    if formula_mismatches:
        preview = "、".join(formula_mismatches[:10])
        more = f"，另有 {len(formula_mismatches) - 10} 个" if len(formula_mismatches) > 10 else ""
        raise ReportGenerationError(f"统一毛利报表写后回读失败：公式未正确写入 {preview}{more}")
    if value_mismatches:
        preview = "、".join(value_mismatches[:10])
        more = f"，另有 {len(value_mismatches) - 10} 个" if len(value_mismatches) > 10 else ""
        raise ReportGenerationError(f"统一毛利报表写后回读失败：固定值未正确写入 {preview}{more}")

    if (
        len(actual_source_rows) != len(expected_source_rows)
        or not actual_source_rows
        or actual_source_rows[0][:49] != expected_source_rows[0][:49]
    ):
        raise ReportGenerationError("统一毛利报表写后回读失败：数据源标题或行数不一致")
    for row_number, (actual, expected) in enumerate(
        zip(actual_source_rows[1:], expected_source_rows[1:]), start=2
    ):
        if any(
            column >= len(actual) or not _sheet_cell_matches(actual[column], expected[column])
            for column in range(49)
        ):
            raise ReportGenerationError(f"统一毛利报表写后回读失败：数据源第 {row_number} 行不一致")

    if (
        len(actual_check_rows) != len(expected_check_rows)
        or not actual_check_rows
        or actual_check_rows[0][:5] != expected_check_rows[0][:5]
    ):
        raise ReportGenerationError("统一毛利报表写后回读失败：检查表标题、行数或进行中标记不一致")
    for row_number, (actual, expected) in enumerate(
        zip(actual_check_rows[1:], expected_check_rows[1:]), start=2
    ):
        if any(
            column >= len(actual) or not _sheet_cell_matches(actual[column], expected[column])
            for column in range(3)
        ):
            raise ReportGenerationError(f"统一毛利报表写后回读失败：检查表第 {row_number} 行不一致")


async def generate(period: str, commit: bool = False) -> dict[str, Any]:
    """Preview or create one monthly report. Mapping validation always precedes writes."""
    period, month = _period_parts(period)
    if not commit:
        report_records, maintenance_records, cost_records = await _load_sources()
        prepared = prepare_report(period, report_records, maintenance_records, cost_records)
        return {"status": "ok", "mode": "preview", **prepared["summary"]}

    async with _generation_lock(period):
        token = await _report_token()
        existing = await _find_existing_report(token, period, month)
        report_records, maintenance_records, cost_records = await _load_sources()
        prepared = prepare_report(period, report_records, maintenance_records, cost_records)
        base = {"status": "ok", "mode": "commit", **prepared["summary"]}
        expected_hash = report_content_hash(prepared)
        owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        claim = await db.claim_unified_report_generation(period, owner, expected_hash)
        if not claim.get("claimed"):
            if claim.get("status") == "complete":
                target = existing or claim
                required = (target.get("wiki_token"), target.get("spreadsheet_token"), target.get("url"))
                marker_matches = bool(
                    existing
                    and existing.get("complete")
                    and existing.get("content_hash") == expected_hash
                )
                if claim.get("content_hash") == expected_hash and marker_matches and all(required):
                    return {
                        **base,
                        "deduped": True,
                        "content_hash": expected_hash,
                        "wiki_token": target["wiki_token"],
                        "spreadsheet_token": target["spreadsheet_token"],
                        "url": target["url"],
                    }
                raise ReportGenerationError("月报生成记录与飞书完成标记不一致，已停止避免重复建表")
            raise ReportGenerationInProgressError(f"{month} 统一毛利月报正在生成，请稍后重试")

        try:
            target = existing or await _copy_or_resume_report(token, period, month)
            await db.set_unified_report_target(
                period,
                owner,
                target["wiki_token"],
                target["spreadsheet_token"],
                target["url"],
            )
            if target.get("complete") and target.get("content_hash") == expected_hash:
                await db.complete_unified_report_generation(period, owner, expected_hash)
                return {
                    **base,
                    "deduped": True,
                    "content_hash": expected_hash,
                    "wiki_token": target["wiki_token"],
                    "spreadsheet_token": target["spreadsheet_token"],
                    "url": target["url"],
                }
            verification = await _write_report(token, target, prepared)
            await db.complete_unified_report_generation(period, owner, verification["content_hash"])
            return {
                **base,
                "deduped": False,
                "wiki_token": target["wiki_token"],
                "spreadsheet_token": target["spreadsheet_token"],
                "url": target["url"],
                **verification,
            }
        except Exception as exc:
            try:
                await db.fail_unified_report_generation(period, owner, str(exc))
            except Exception:
                pass
            raise
