"""September's audited Orders/Bill bridge. No payment or inventory mutations.

The export's R is a settlement reversal, not a product-sales refund. Keep
both meanings in the bridge; never infer shipping by forcing it to balance.
The claim exceptions are bound to the three audited exports, not reusable
assumptions about future BPP (buyer protection) cases.
"""
from collections import defaultdict
from decimal import Decimal
import hashlib
import io

import openpyxl

VERSION = "2026-09-CBT-OrdersBill-v1"
PERIOD = "month_2026-09"
SHOP = "ML CBT-FULL (1502236229)"
HASHES = {
    "orders": "21f95bc2c63ea5af8007370736563b4c5aff164bf4ff0a28f226bd2c077d2144",
    "bill": "cef8633afc23d6943d25fc8c14c1d04f1d5159b765041ddabcb40db1421e82d0",
    "ads": "2c25f833c3e9cc45ca7d295ed16643c2c9910b782f2a6e36e22f75964775786c",
}
# Official claims 5575036282 / 5571629636: total/partial buyer coverage;
# seller retains settlement. Quantity is not a USD payment amount or restock.
COVERED_REFUND_UNITS = {"2000018358859318": 1, "2000018275632126": 1}
CREDIT_TYPES = {
    "Selling fee refund": "commission",
    "Anulación del cargo por transferencia de dinero a cuenta internacional": "commission",
    "Cancellation of Mercado Envios fee": "shipping",
}


def number(value):
    if value is None or str(value).strip() in ("", "-"):
        return Decimal(0)
    value = Decimal(str(value))
    if not value.is_finite():
        raise RuntimeError("CBT 金额不是有限数")
    return value


def ident(value):
    return str(int(value)) if isinstance(value, (int, float)) else str(value or "").strip()


def rows(data, sheet, start):
    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        return list(book[sheet].iter_rows(min_row=start, values_only=True))
    finally:
        book.close()


def reconcile(orders_data, bill_data, ads_data, month):
    if month != "2026-09":
        raise RuntimeError("CBT 已核实的售后例外只适用于 2026-09")
    for kind, data in (("orders", orders_data), ("bill", bill_data), ("ads", ads_data)):
        if hashlib.sha256(data).hexdigest() != HASHES[kind]:
            raise RuntimeError(f"CBT {kind} 导出已变更；需重新核实售后证据，旧版不覆盖")
    order_rows = [r for r in rows(orders_data, "Orders US", 7) if r[1] and r[2]]
    bill_rows = [r for r in rows(bill_data, "REPORT", 9) if r[2]]
    fee_ids = {ident(r[2]): r for r in bill_rows}
    if len(fee_ids) != len(bill_rows):
        raise RuntimeError("CBT 账单费用 ID 重复")
    by_order = defaultdict(list)
    credits = defaultdict(lambda: defaultdict(Decimal))
    return_fees = Decimal(0)
    for r in bill_rows:
        oid = ident(r[14])
        by_order[oid].append(r)
        if r[3] == "Return fee":
            return_fees += number(r[7])
        if r[3] not in CREDIT_TYPES:
            continue
        original = fee_ids.get(ident(r[6]))
        if (not original or ident(original[14]) != oid
                or number(original[7]) <= 0 or number(original[7]) + number(r[7]) != 0):
            raise RuntimeError(f"CBT 退费无法对应原收费：{ident(r[2])}")
        credits[oid][CREDIT_TYPES[r[3]]] -= number(r[7])

    parents = {ident(r[0]): r for r in order_rows if not ident(r[24]) and not number(r[9])}
    children = defaultdict(list)
    for r in order_rows:
        if ident(r[24]) and ident(r[0]) in parents:
            children[ident(r[0])].append(r)
    normalized = []
    for r in order_rows:
        if not ident(r[24]):
            if ident(r[0]) not in children:
                raise RuntimeError("CBT 组合包缺少子订单，禁止全店摊分")
            continue
        oid, qty, pack = ident(r[1]), number(r[9]), ident(r[0])
        vals = {k: number(r[i]) for k, i in {"K": 10, "L": 11, "M": 12, "N": 13,
                "O": 14, "P": 15, "Q": 16, "R": 17, "S": 18}.items()}
        fees = [b for b in by_order[oid] if b[3] in ("Selling Fee", "Service fee") and number(b[7]) > 0]
        if pack in parents:
            if not fees or len({number(b[22]) for b in fees}) != 1:
                raise RuntimeError(f"CBT 组合包子单缺少销售额证据：{oid}")
            parent = parents[pack]
            gross = number(fees[0][22])
            share = gross / number(parent[10])
            # Revenue/commission are observed child amounts. Remaining shared
            # package amounts use a stated revenue allocation within this pack.
            vals = {k: (number(parent[i]) * share).quantize(Decimal("0.01")) for k, i in {"M": 12, "N": 13,
                    "O": 14, "P": 15, "Q": 16, "R": 17}.items()}
            vals.update(K=gross, L=-sum(number(b[7]) for b in fees))
            vals["S"] = sum(vals.values())
        elif qty > 1:
            if not fees or any(number(b[20]) != qty for b in fees):
                raise RuntimeError(f"CBT 多件订单缺少账单数量证据：{oid}")
            totals = {number(b[22]) for b in fees}
            if len(totals) != 1:
                raise RuntimeError(f"CBT 多件订单账单销售额冲突：{oid}")
            billed = totals.pop()
            choices = [m for m in (Decimal(1), qty)
                       if abs((vals["K"] + vals["M"]) * m - billed) <= Decimal("0.02")]
            if len(choices) != 1:
                raise RuntimeError(f"CBT 多件销售额无法唯一核实：{oid}")
            vals["K"] *= choices[0]
            vals["M"] *= choices[0]
        status = str(r[4] or "")
        canceled = status in ("Canceled by the buyer", "Package canceled by Mercado Libre")
        returned = status.startswith(("Return completed. We put the product up for sale again", "Return inspected"))
        zero_refund = canceled or returned
        if zero_refund and vals["S"] != 0:
            raise RuntimeError(f"CBT 已取消/全退订单非零结算：{oid}")
        refund_qty = qty if zero_refund else Decimal(COVERED_REFUND_UNITS.get(oid, 0))
        product_refund = vals["K"] * refund_qty / qty
        cc, sc = credits[oid]["commission"], credits[oid]["shipping"]
        residual = vals["S"] - sum(vals[k] for k in ("K", "L", "M", "N", "O", "P", "Q", "R"))
        if abs(residual) > Decimal("0.02"):
            raise RuntimeError(f"CBT 逐订单结算差额未解释：{oid} 差额={residual}")
        reclassification = product_refund + vals["R"] - cc - sc
        normalized.append(dict(sku=str(r[24]), title=str(r[26] or ""), listing=ident(r[25]),
            oid=oid, units=qty, net_units=qty-refund_qty, returned_units=qty if returned else Decimal(0),
            K=vals["K"], L=-vals["L"]-cc, O=-vals["O"]-vals["P"]-vals["N"]-sc,
            Q=-vals["Q"], R=product_refund, S=vals["S"], adjustment=vals["M"]+reclassification+residual,
            fx_margin=vals["M"], refund_reclassification=reclassification, source_rounding=residual))
    aggregate = defaultdict(lambda: defaultdict(Decimal))
    listing2sku = {}
    for row in normalized:
        a = aggregate[row["sku"]]
        for key, value in row.items():
            if isinstance(value, Decimal):
                a[key] += value
        a["orders"] += 1
        a["title"] = row["title"]
        listing2sku[row["listing"]] = row["sku"]
    for r in bill_rows:
        if r[3] == "Return fee":
            sku = listing2sku.get(ident(r[26]))
            if not sku:
                raise RuntimeError("CBT 退货处理费无法对应商品，需独立归类")
            aggregate[sku]["return_fee"] += number(r[7])
    totals = {k: round(float(sum(a[k] for a in aggregate.values())), 2) for k in (
        "units", "net_units", "returned_units", "K", "L", "O", "Q", "R", "S", "adjustment",
        "fx_margin", "refund_reclassification", "source_rounding")}
    expected = dict(units=459, net_units=436, returned_units=7, K=13711.69, L=2046.33,
                    O=1605.92, Q=1890.17, R=602.09, S=7728.47, adjustment=161.29)
    if any(abs(totals[k]-v) > 0.011 for k, v in expected.items()) or return_fees != Decimal("11.79"):
        raise RuntimeError(f"CBT 复核总数与独立取证不符：{totals}")
    return ({sku: {k: float(v) if isinstance(v, Decimal) else v for k, v in a.items()}
             for sku, a in aggregate.items()}, listing2sku,
            {"version": VERSION, "hashes": HASHES, "totals": totals, "return_fees": float(return_fees),
             "orders": [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()}
                        for r in normalized]})
