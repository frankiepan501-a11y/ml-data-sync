# -*- coding: utf-8 -*-
"""CBT-FULL 官方导出(B) → 飞书报表 逐SKU全口径解析 (云端版, 2026-06-18 task3云化).

背景: CBT API 被 429 墙在 ~85% 缓存 + Full仓储无 API → 纯 API 做不到准确 CBT 毛利.
官方导出(Orders+账单+广告)是唯一完整准确源(B). 俊辉每月把3文件传飞书云盘文件夹 → 本模块下载解析 →
按SKU update 飞书 CBT 行(保留美通头程/海外仓列). 本地版前身 C:/tmp/cbt_export_ingest.py.

口径(USD): 锚定导出 S列(净受领, 已含运费收入N/汇率M/额外运费P). 全额毛利=飞书公式字段(本模块不写).
RMB = USD × fx. 采购 = 领星 cg_price × units(走 lingxing).
"""
import calendar
import io, os, json, time, re, math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import httpx
import openpyxl

from app import lingxing

FEISHU = "https://open.feishu.cn/open-apis"
APP_TOKEN = "WM3LbBr76aRqMys2of8c1dGInEb"
TABLE_ID = "tbl09sRPkX35PDfU"
SHOP_LABEL = "ML CBT-FULL (1502236229)"
F_FULL = "Full仓储费(RMB)"

# Orders report 列(0-based, row8起数据). N(13)=运费收入(S列已含,不单列), S(18)=净受领锚点.
O_UNITS, O_K, O_L, O_N, O_O, O_Q, O_R, O_S, O_SKU, O_LISTING, O_TITLE = 9,10,11,13,14,16,17,18,24,25,26


async def _ft() -> str:
    app_id = os.getenv("FEISHU_APP_ID", "cli_a9f6ae86fce8dbd8")
    secret = os.getenv("FEISHU_APP_SECRET", "")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{FEISHU}/auth/v3/tenant_access_token/internal",
                         json={"app_id": app_id, "app_secret": secret})
    payload = r.json()
    if r.status_code != 200 or payload.get("code") not in (None, 0) or not payload.get("tenant_access_token"):
        raise RuntimeError(
            f"飞书授权失败：status={r.status_code} code={payload.get('code')}"
        )
    return payload["tenant_access_token"]


async def _list_folder(tok: str, folder_token: str) -> list[dict]:
    out = []
    page = None
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            url = f"{FEISHU}/drive/v1/files?folder_token={folder_token}&page_size=50" + (f"&page_token={page}" if page else "")
            response = await c.get(url, headers={"Authorization": f"Bearer {tok}"})
            payload = response.json()
            if response.status_code != 200 or payload.get("code") != 0:
                raise RuntimeError(
                    f"飞书导出文件夹读取失败：status={response.status_code} code={payload.get('code')}"
                )
            d = payload.get("data", {})
            out += d.get("files", [])
            page = d.get("next_page_token")
            if not d.get("has_more") or not page:
                break
    return out


async def _download(tok: str, file_token: str) -> bytes:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.get(f"{FEISHU}/drive/v1/files/{file_token}/download", headers={"Authorization": f"Bearer {tok}"})
        r.raise_for_status()
        return r.content


def _candidate_files(files: list[dict], *kws) -> list[dict]:
    candidates = [
        f for f in files
        if f.get("type") == "file" and any(k in (f.get("name") or "") for k in kws)
    ]
    candidates.sort(key=lambda f: f.get("modified_time", "0"), reverse=True)
    return candidates


def _month_bounds(month: str) -> tuple[date, date]:
    try:
        year, month_number = (int(part) for part in month.split("-"))
        return date(year, month_number, 1), date(
            year,
            month_number,
            calendar.monthrange(year, month_number)[1],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid month {month!r}; expected YYYY-MM") from exc


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in (
        "%B %d, %Y %I:%M %p GMT%z",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _orders_period_evidence(data: bytes, month: str) -> dict:
    start, end = _month_bounds(month)
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook["Orders US"] if "Orders US" in workbook.sheetnames else workbook.worksheets[0]
    dates = [
        parsed
        for row in sheet.iter_rows(min_row=7, values_only=True)
        if (parsed := _parse_date(row[2] if len(row) > 2 else None))
    ]
    workbook.close()
    counts = Counter(d.strftime("%Y-%m") for d in dates)
    target_dates = [d for d in dates if start <= d <= end]
    share = len(target_dates) / len(dates) if dates else 0.0
    within_boundary = bool(dates) and min(dates) >= start - timedelta(days=2) and max(dates) <= end + timedelta(days=2)
    covers_month = bool(target_dates) and min(target_dates).day <= 2 and max(target_dates).day >= end.day - 1
    ok = share >= 0.85 and within_boundary and covers_month
    return {
        "ok": ok,
        "rows_with_dates": len(dates),
        "target_rows": len(target_dates),
        "target_share": round(share, 4),
        "date_min": min(dates).isoformat() if dates else None,
        "date_max": max(dates).isoformat() if dates else None,
        "month_counts": dict(sorted(counts.items())),
        "reason": None if ok else "Orders 日期主体或月初/月末覆盖范围与目标月份不匹配",
    }


def _ads_period_evidence(data: bytes, month: str) -> dict:
    start, end = _month_bounds(month)
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook["Report by ads"] if "Report by ads" in workbook.sheetnames else workbook.worksheets[-1]
    spans = []
    for row in sheet.iter_rows(min_row=3, values_only=True):
        left = _parse_date(row[0] if len(row) > 0 else None)
        right = _parse_date(row[1] if len(row) > 1 else None)
        if left or right:
            spans.append((left, right))
    workbook.close()
    ok = bool(spans) and all(left == start and right == end for left, right in spans)
    unique_spans = sorted({
        ((left.isoformat() if left else None), (right.isoformat() if right else None))
        for left, right in spans
    })
    return {
        "ok": ok,
        "rows_with_period": len(spans),
        "periods": unique_spans,
        "reason": None if ok else "广告报表 From/To 与目标月份不匹配",
    }


def _bill_period_evidence(data: bytes, month: str) -> dict:
    start, end = _month_bounds(month)
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    if "REPORT" not in workbook.sheetnames:
        workbook.close()
        return {"ok": False, "reason": "账单缺少 REPORT 工作表"}
    sheet = workbook["REPORT"]
    dates = [
        parsed
        for row in sheet.iter_rows(min_row=9, values_only=True)
        if (parsed := _parse_date(row[1] if len(row) > 1 else None))
    ]
    workbook.close()
    ok = bool(dates) and min(dates) == start and max(dates) == end and all(start <= d <= end for d in dates)
    return {
        "ok": ok,
        "rows_with_dates": len(dates),
        "date_min": min(dates).isoformat() if dates else None,
        "date_max": max(dates).isoformat() if dates else None,
        "reason": None if ok else "账单费用日期范围与目标月份不匹配",
    }


async def _pick_for_month(tok: str, files: list[dict], month: str, kind: str,
                          validator, *kws) -> tuple[dict | None, bytes | None, dict]:
    rejected = []
    for candidate in _candidate_files(files, *kws):
        data = await _download(tok, candidate["token"])
        try:
            evidence = validator(data, month)
        except Exception as exc:
            evidence = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if evidence.get("ok"):
            return candidate, data, evidence
        rejected.append({"name": candidate.get("name"), **evidence})
    return None, None, {"ok": False, "kind": kind, "rejected": rejected}


def _num(r, i):
    return r[i] if (i < len(r) and isinstance(r[i], (int, float))) else 0.0


def _parse_orders(data: bytes, month: str):
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb["Orders US"] if "Orders US" in wb.sheetnames else wb.worksheets[0]
    # The selected workbook has already passed target-month evidence validation.
    # Treat the official export itself as source B, including its timezone boundary
    # rows (for example Jul-31 displayed in an August export).
    rows = [
        row for row in ws.iter_rows(min_row=7, max_row=ws.max_row, values_only=True)
        if _parse_date(row[2] if len(row) > 2 else None)
    ]
    wb.close()
    listing2sku = {}
    for r in rows:
        if r[O_SKU] and r[O_LISTING]:
            listing2sku[str(r[O_LISTING])] = str(r[O_SKU])
    agg = defaultdict(lambda: dict(units=0.0, K=0.0, L=0.0, N=0.0, O=0.0, Q=0.0, R=0.0, S=0.0, orders=0, title=""))
    for r in rows:
        sku = r[O_SKU]
        if not sku:
            sku = listing2sku.get(str(r[O_LISTING])) or "(组合包)"
        a = agg[str(sku)]
        a["units"] += _num(r, O_UNITS); a["K"] += _num(r, O_K); a["L"] += _num(r, O_L)
        a["N"] += _num(r, O_N); a["O"] += _num(r, O_O); a["Q"] += _num(r, O_Q)
        a["R"] += _num(r, O_R); a["S"] += _num(r, O_S); a["orders"] += 1
        if not a["title"] and r[O_TITLE]:
            a["title"] = str(r[O_TITLE])
    return agg, listing2sku


def _parse_ads(data: bytes, listing2sku: dict):
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb["Report by ads"] if "Report by ads" in wb.sheetnames else wb.worksheets[-1]
    ad = defaultdict(float); total = 0.0; unmatched = 0.0
    for r in ws.iter_rows(min_row=3, max_row=ws.max_row, values_only=True):
        lst = r[4] if len(r) > 4 else None
        inv = r[12] if (len(r) > 12 and isinstance(r[12], (int, float))) else 0.0
        if not inv:
            continue
        total += inv
        sku = listing2sku.get(str(lst)) if lst else None
        if sku:
            ad[sku] += inv
        else:
            unmatched += inv
    wb.close()
    return dict(ad), total, unmatched


def _parse_storage(data: bytes) -> float:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb["REPORT"]
    KW = ["仓陈旧", "Long-term storage", "存储服务费", "入库货件违约", "Storage service fee", "almacenamiento"]
    total = 0.0
    for r in ws.iter_rows(min_row=9, max_row=ws.max_row, values_only=True):
        d = r[3] if len(r) > 3 else None
        v = r[7] if (len(r) > 7 and isinstance(r[7], (int, float))) else 0.0
        if d is None or not v:
            continue
        if any(k in str(d) for k in KW):
            total += v
    wb.close()
    return total


async def _bitable_all(tok: str) -> list[dict]:
    out = []
    page_token = None
    seen_tokens = set()
    async with httpx.AsyncClient(timeout=60) as c:
        while True:
            # Use LIST API for full-table reads. records/search pagination is not
            # stable without sort and has previously dropped ML cost rows.
            url = f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records?page_size=500"
            if page_token:
                url += f"&page_token={page_token}"
            response = await c.get(url, headers={"Authorization": f"Bearer {tok}"})
            payload = response.json()
            if response.status_code != 200 or payload.get("code") != 0:
                raise RuntimeError(
                    f"CBT 飞书报表读取失败：status={response.status_code} code={payload.get('code')}"
                )
            d = payload.get("data", {})
            out += d.get("items", [])
            page_token = d.get("page_token")
            if not d.get("has_more"):
                break
            if not page_token or page_token in seen_tokens:
                raise RuntimeError("CBT 飞书报表分页游标异常；停止写入")
            seen_tokens.add(page_token)
    return out


async def _ensure_full_field(tok: str):
    async with httpx.AsyncClient(timeout=30) as c:
        response = await c.get(
            f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/fields?page_size=200",
            headers={"Authorization": f"Bearer {tok}"},
        )
        payload = response.json()
        if response.status_code != 200 or payload.get("code") != 0:
            raise RuntimeError(
                f"CBT 飞书字段读取失败：status={response.status_code} code={payload.get('code')}"
            )
        d = payload.get("data", {})
        if F_FULL not in {f["field_name"] for f in d.get("items", [])}:
            raise RuntimeError(f"CBT 飞书缺少必需字段 {F_FULL}；未写入数据")


async def run(month: str, commit: bool = False, fx: float = 6.8628,
              folder_token: str | None = None,
              preserve_existing_as: str | None = None) -> dict:
    folder_token = folder_token or os.getenv("CBT_EXPORT_FOLDER_TOKEN", "")
    if not folder_token:
        return {"status": "error", "msg": "CBT_EXPORT_FOLDER_TOKEN 未配置"}
    period = f"month_{month}"
    tok = await _ft()
    files = await _list_folder(tok, folder_token)
    f_ord, ord_data, ord_evidence = await _pick_for_month(
        tok, files, month, "orders", _orders_period_evidence, "Orders", "Ventas", "Mercado_Libre"
    )
    f_ad, ad_data, ad_evidence = await _pick_for_month(
        tok, files, month, "ads", _ads_period_evidence, "report-pads", "广告", "pads"
    )
    f_bill, bill_data, bill_evidence = await _pick_for_month(
        tok, files, month, "bill", _bill_period_evidence, "BILL", "账单"
    )
    validations = {"orders": ord_evidence, "ads": ad_evidence, "bill": bill_evidence}
    if not all((f_ord, f_ad, f_bill)):
        missing = [kind for kind, picked in (("Orders", f_ord), ("广告", f_ad), ("账单", f_bill)) if not picked]
        return {
            "status": "error",
            "msg": f"CBT {month} 官方导出月份校验失败：{', '.join(missing)}没有匹配文件；飞书未写入",
            "month": month,
            "commit": commit,
            "file_validations": validations,
        }

    agg, l2s = _parse_orders(ord_data, month)
    ad_sku, ad_total, ad_unmatched = _parse_ads(ad_data, l2s)
    storage_total = _parse_storage(bill_data)

    # 组合包空SKU行(0件)按营收摊到真实SKU
    blanks = [k for k in agg if str(k).strip() == "" or k == "(组合包)"]
    if blanks:
        real = [(k, v) for k, v in agg.items() if k not in blanks]
        rk = sum(v["K"] for _, v in real) or 1
        for bk in blanks:
            b = agg.pop(bk)
            for k, v in real:
                sh = v["K"] / rk
                for fld in ("K", "L", "N", "O", "Q", "R", "S"):
                    v[fld] += b[fld] * sh

    # 领星 cg
    products = await lingxing.fetch_all_products()
    def cg_for(sku):
        p = products.get(lingxing.resolve_erp_sku(sku))
        try:
            return float(p["cg_price"]) if p and p.get("cg_price") is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    tot_K = sum(a["K"] for a in agg.values()) or 1
    rows_out = []
    for sku, a in agg.items():
        K, L, Q, R, S = a["K"], -a["L"], -a["Q"], -a["R"], a["S"]
        O = a["K"] + a["L"] + a["Q"] + a["R"] - a["S"]  # net 物流(反推闭合到S)
        share = K / tot_K
        ad_alloc = ad_sku.get(sku, 0.0) + ad_unmatched * share
        storage_alloc = storage_total * share
        caigou = cg_for(sku) * a["units"]
        rows_out.append(dict(sku=sku, units=a["units"], orders=a["orders"], title=a["title"],
                             K=K, L=L, O=O, Q=Q, R=R, ad=ad_alloc, storage=storage_alloc, caigou=caigou,
                             has_cg=(cg_for(sku) > 0)))

    tot = {k: round(sum(r[k] for r in rows_out), 2) for k in ("K", "L", "O", "Q", "R", "ad", "storage", "caigou")}
    tot["units"] = int(sum(r["units"] for r in rows_out))
    summary = {"status": "ok", "month": month, "commit": commit, "fx": fx,
               "files": {"orders": f_ord["name"], "ads": (f_ad or {}).get("name"), "bill": (f_bill or {}).get("name")},
               "file_validations": validations,
               "sku_count": len(rows_out), "totals": tot,
               "nocg_skus": [r["sku"] for r in rows_out if not r["has_cg"] and r["units"] > 0]}
    if not commit:
        summary["note"] = "DRY-RUN 未写飞书; commit=true 才按SKU update"
        return summary

    # 安全生成整个 CBT/月快照：新行先创建并逐 SKU 核验；旧行不删除，
    # 而是改到显式指定的历史周期标签，确保修复前后的证据可以并存。
    await _ensure_full_field(tok)
    def _txt(v): return v[0].get("text", "") if isinstance(v, list) and v else (str(v) if v is not None else "")
    def _scope(items, target_period=period):
        return [
            item for item in items
            if _txt((item.get("fields") or {}).get("店铺")) == SHOP_LABEL
            and _txt((item.get("fields") or {}).get("周期")) == target_period
        ]

    all_items = await _bitable_all(tok)
    existing_items = _scope(all_items)
    existing_by_sku = {
        _txt((item.get("fields") or {}).get("SKU")): item
        for item in existing_items
    }
    now_ms = int(time.time() * 1000)
    fresh_records = []
    seen = set()
    preserved_cost_fields = ("头程成本(RMB)", "海外仓成本(RMB)")
    for r in rows_out:
        sku = r["sku"]
        seen.add(sku)
        fld = {
            "SKU": sku, "平台": "Mercado Libre", "店铺": SHOP_LABEL, "周期": period,
            "订单数": int(r["orders"]), "件数": int(r["units"]), "币种": "USD", "我的汇率": round(fx, 4),
            "营收(原币)": round(r["K"], 2), "营收(RMB)": round(r["K"]*fx, 2),
            "客单价(原币)": round(r["K"]/r["orders"], 2) if r["orders"] else 0,
            "ML佣金(原币)": round(r["L"], 2), "ML佣金(RMB)": round(r["L"]*fx, 2),
            "物流费(原币)": round(r["O"], 2), "物流费(RMB)": round(r["O"]*fx, 2),
            "VAT估算(原币)": round(r["Q"], 2), "VAT估算(RMB)": round(r["Q"]*fx, 2),
            "退款金额(原币)": round(r["R"], 2), "退款金额(RMB)": round(r["R"]*fx, 2),
            "广告费(原币)": round(r["ad"], 2), "广告费(RMB)": round(r["ad"]*fx, 2),
            "采购成本(RMB)": round(r["caigou"], 2), F_FULL: round(r["storage"]*fx, 2),
            "卖家折扣(原币)": 0, "卖家折扣(RMB)": 0,
            "简易毛利(RMB)": round(r["K"]*fx - r["caigou"], 2),
            "商品标题": r["title"], "数据拉取时间": now_ms,
        }
        old_fields = (existing_by_sku.get(sku) or {}).get("fields") or {}
        for field_name in preserved_cost_fields:
            if old_fields.get(field_name) is not None:
                fld[field_name] = old_fields[field_name]
        numeric_fields = {
            "订单数", "件数", "我的汇率", "客单价(原币)", "营收(原币)", "营收(RMB)",
            "ML佣金(原币)", "ML佣金(RMB)", "物流费(原币)", "物流费(RMB)",
            "VAT估算(原币)", "VAT估算(RMB)", "退款金额(原币)", "退款金额(RMB)",
            "广告费(原币)", "广告费(RMB)", "采购成本(RMB)", F_FULL,
            "卖家折扣(原币)", "卖家折扣(RMB)", "简易毛利(RMB)",
            "头程成本(RMB)", "海外仓成本(RMB)",
        }
        for field_name in numeric_fields.intersection(fld):
            try:
                number = float(fld[field_name])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"CBT SKU={sku} 数字字段无法转换：{field_name}") from exc
            if not math.isfinite(number):
                raise RuntimeError(f"CBT SKU={sku} 数字字段不是有限数：{field_name}")
            fld[field_name] = int(number) if field_name in {"订单数", "件数"} else number
        fresh_records.append({"fields": fld})

    if len(existing_items) > 500 or len(fresh_records) > 500:
        raise RuntimeError("CBT 飞书安全生成超过单批 500 行限制；旧数据未修改")

    verify_fields = (
        "订单数", "件数", "我的汇率", "客单价(原币)",
        "营收(原币)", "营收(RMB)", "ML佣金(原币)", "ML佣金(RMB)",
        "物流费(原币)", "物流费(RMB)",
        "VAT估算(原币)", "退款金额(原币)", "广告费(原币)",
        "VAT估算(RMB)", "退款金额(RMB)", "广告费(RMB)",
        "卖家折扣(原币)", "卖家折扣(RMB)", "采购成本(RMB)",
        "简易毛利(RMB)", F_FULL, "头程成本(RMB)", "海外仓成本(RMB)",
    )
    verify_text_fields = ("SKU", "平台", "店铺", "周期", "币种", "商品标题")

    def _verify_items(items, phase):
        if len(items) != len(fresh_records):
            raise RuntimeError(
                f"CBT 飞书{phase}失败：expected_rows={len(fresh_records)} actual_rows={len(items)}"
            )
        expected_by_sku = {record["fields"]["SKU"]: record["fields"] for record in fresh_records}
        actual_by_sku = {}
        for item in items:
            sku = _txt((item.get("fields") or {}).get("SKU"))
            if sku in actual_by_sku:
                raise RuntimeError(f"CBT 飞书{phase}失败：SKU={sku} 重复")
            actual_by_sku[sku] = item.get("fields") or {}
        if set(actual_by_sku) != set(expected_by_sku):
            raise RuntimeError(f"CBT 飞书{phase}失败：SKU 集合不一致")
        for sku, expected_fields in expected_by_sku.items():
            actual_fields = actual_by_sku[sku]
            for field_name in verify_text_fields:
                if _txt(actual_fields.get(field_name)) != _txt(expected_fields.get(field_name)):
                    raise RuntimeError(
                        f"CBT 飞书{phase}失败：SKU={sku} field={field_name} 文本不一致"
                    )
            for field_name in verify_fields:
                expected = float(expected_fields.get(field_name) or 0)
                actual = float(actual_fields.get(field_name) or 0)
                if abs(actual - expected) > 0.05:
                    raise RuntimeError(
                        f"CBT 飞书{phase}失败：SKU={sku} field={field_name} "
                        f"expected={expected} actual={actual}"
                    )

    # 完全相同的重复请求直接返回，避免生成重复行。
    if existing_items:
        try:
            _verify_items(existing_items, "现有快照核验")
        except RuntimeError:
            pass
        else:
            summary.update({
                "created": 0,
                "rows_archived": 0,
                "old_version_period": None,
                "stale_rows_archived": 0,
                "stale_skus_archived": [],
                "rows_verified": len(existing_items),
                "unchanged": True,
            })
            return summary

    if existing_items:
        if not preserve_existing_as:
            raise RuntimeError(
                "CBT 当前周期已有不同数据；必须传 preserve_existing_as 保存旧版证据后才能生成修正版"
            )
        if preserve_existing_as == period or not re.fullmatch(r"month_\d{4}-\d{2}_[A-Za-z0-9_-]+", preserve_existing_as):
            raise RuntimeError("preserve_existing_as 格式不安全，示例：month_2026-08_original_20260907")
        if _scope(all_items, preserve_existing_as):
            raise RuntimeError(f"CBT 旧版证据周期已存在：{preserve_existing_as}；未修改任何数据")

    async with httpx.AsyncClient(timeout=60) as c:
        H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        create_url = f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/batch_create"
        delete_url = f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/batch_delete"
        update_url = f"{FEISHU}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/batch_update"

        async def _post_checked(url, body, action):
            response = await c.post(url, json=body, headers=H)
            payload = response.json()
            if response.status_code != 200 or payload.get("code") != 0:
                raise RuntimeError(
                    f"CBT 飞书{action}失败：status={response.status_code} code={payload.get('code')}"
                )
            return payload

        async def _delete_checked(record_ids, action):
            if record_ids:
                await _post_checked(delete_url, {"records": record_ids}, action)

        async def _relabel_checked(record_ids, target_period, action):
            if record_ids:
                await _post_checked(
                    update_url,
                    {"records": [
                        {"record_id": record_id, "fields": {"周期": target_period}}
                        for record_id in record_ids
                    ]},
                    action,
                )

        created = await c.post(create_url, json={"records": fresh_records}, headers=H)
        created_payload = created.json()
        if created.status_code != 200 or created_payload.get("code") != 0:
            raise RuntimeError(
                "CBT 飞书新快照创建失败，旧数据已保留："
                f"status={created.status_code} code={created_payload.get('code')} msg={created_payload.get('msg')}"
            )
        created_ids = [
            item.get("record_id")
            for item in (created_payload.get("data", {}).get("records") or [])
            if item.get("record_id")
        ]
        if len(created_ids) != len(fresh_records):
            rollback_error = None
            try:
                await _delete_checked(created_ids, "不完整新快照回滚")
            except RuntimeError as exc:
                rollback_error = str(exc)
            raise RuntimeError(
                "CBT 飞书新快照返回行数不完整；"
                + ("新行已回滚，旧数据已保留" if not rollback_error else f"新行回滚失败：{rollback_error}")
            )

        created_id_set = set(created_ids)

        try:
            pre_archive = _scope(await _bitable_all(tok))
            created_items = [item for item in pre_archive if item.get("record_id") in created_id_set]
            _verify_items(created_items, "写入前核验")
        except Exception as verify_error:
            try:
                await _delete_checked(created_ids, "写入前核验回滚")
            except RuntimeError as rollback_error:
                raise RuntimeError(f"{verify_error}；且新行回滚失败：{rollback_error}") from verify_error
            raise RuntimeError(f"CBT 飞书写入前回读/核验失败：{verify_error}；新行已回滚") from verify_error

        existing_ids = [item.get("record_id") for item in existing_items if item.get("record_id")]
        if existing_ids:
            try:
                await _relabel_checked(existing_ids, preserve_existing_as, "旧版证据归档")
            except RuntimeError as archive_error:
                try:
                    await _delete_checked(created_ids, "归档失败后的新行回滚")
                except RuntimeError as rollback_error:
                    raise RuntimeError(f"{archive_error}；且新行回滚失败：{rollback_error}") from archive_error
                raise

        try:
            after_archive = await _bitable_all(tok)
            final_items = _scope(after_archive)
            archived_items = _scope(after_archive, preserve_existing_as) if existing_ids else []
            if {item.get("record_id") for item in final_items} != created_id_set:
                raise RuntimeError(
                    f"CBT 飞书最终回读不一致：expected_rows={len(created_ids)} actual_rows={len(final_items)}"
                )
            if existing_ids and {item.get("record_id") for item in archived_items} != set(existing_ids):
                raise RuntimeError("CBT 飞书旧版证据归档回读不一致")
            _verify_items(final_items, "最终回读核验")
        except Exception as verify_error:
            recovery_errors = []
            try:
                await _delete_checked(created_ids, "最终核验失败后的新行回滚")
            except Exception as exc:
                recovery_errors.append(str(exc))
            try:
                await _relabel_checked(existing_ids, period, "最终核验失败后的旧版恢复")
            except Exception as exc:
                recovery_errors.append(str(exc))
            suffix = "；恢复成功" if not recovery_errors else "；恢复失败：" + " | ".join(recovery_errors)
            raise RuntimeError(f"{verify_error}{suffix}") from verify_error

    stale_skus = [sku for sku in existing_by_sku if sku not in seen]
    summary.update({
        "created": len(created_ids),
        "rows_archived": len(existing_items),
        "old_version_period": preserve_existing_as if existing_items else None,
        "stale_rows_archived": len(stale_skus),
        "stale_skus_archived": stale_skus,
        "rows_verified": len(final_items),
        "unchanged": False,
    })
    return summary
