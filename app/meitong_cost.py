# -*- coding: utf-8 -*-
"""美通中转 头程/海外仓成本 → ML 毛利报表两列(头程成本/海外仓成本)。月度自动灌。

口径(Frankie 2026-05-28, 方案A): 只灌经美通中转的 SKU, 其余留空(待其他货代)。摊分全程不碰美通账单(账单只对账)。
  头程成本   = 订单API(query_orders) 收费重 × 渠道费率(费率快照库)
  海外仓操作费 = 指令明细sheet 实际换标箱数 × 单箱费(海外仓操作费快照库)
  join 运单号 → 活表 gGxKHQ SKU/数量/平台 → 按数量摊到每SKU → 领星映ERP → per件单价×ML当期件数 → 写报表
密钥全走 env(无硬编, 适配公开仓): FEISHU_APP_ID/SECRET, LINGXING_APP_ID/SECRET, MEITONG_USER/PASS。
本地脚本权威源: ~/scripts/meitong/ (meitong_ml_pipeline.py + meitong_ml_write.py)。
"""
import os, json, time, hashlib, base64, urllib.request, urllib.parse, urllib.error, datetime
from collections import defaultdict
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding

# ---- 配置(env) ----
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "cli_a9f6ae86fce8dbd8")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
ML_APP = os.getenv("FEISHU_BASE_APP_TOKEN", "WM3LbBr76aRqMys2of8c1dGInEb")
ML_T = os.getenv("FEISHU_BASE_TABLE_ID", "tbl09sRPkX35PDfU")
SHIP_APP = "K7UZbm86Fa1q5Ksvyq7cEguDnFf"   # 发货进度管理台(费率/海外仓操作费快照库)
RATE_T = "tbl152vrNJuvfJVS"                # 费率快照库
OVS_T = "tbllx2XAalZwZJsF"                 # 海外仓操作费快照库
ZL_SS = "FZe2sGNzuhwv1ztWquJcQzsNnOf"      # 墨西哥发货明细表(电子表格)
ZL_SHEET_CMD = "72g5oE"                    # 指令明细 sheet
ZL_SHEET_LIVE = "gGxKHQ"                   # 本地仓发墨西哥中转仓 sheet
LX_ID = os.getenv("LINGXING_APP_ID", "")
LX_SECRET = os.getenv("LINGXING_APP_SECRET", "")
MEITONG_BASE = "http://56carrier.com"
MEITONG_CLIENT_ID = "aaf"; MEITONG_CLIENT_SECRET = "aaf88888888"
MEITONG_USER = os.getenv("MEITONG_USER", "奥得尔")
MEITONG_PASS = os.getenv("MEITONG_PASS", "")
F_HEAD = "头程成本(RMB)"; F_OVS = "海外仓成本(RMB)"

# 中转仓产品名 → ERP sku 别名(梁俊辉 2026-05-28 核验17款; 发光手柄2月全霍尔)
ALIAS_TO_ERP = {
    "YS11发光pro手柄-涂鸦": "FF01A-01", "YS11发光pro手柄-白眼款": "FF01B-01", "YS11发光pro手柄-猫爪款": "FF01C-01",
    "YS47发光joycon手柄-涂鸦": "FF02A-03", "YS47发光joycon手柄-波纹款": "FF02A-01",
    "YS11-5蓝牙发光手柄+充电底座套装-惊奇款": "FF05B-01",
    "Switch2潜水员戴夫收纳包（LOGO）+2代switch摇杆帽-戴夫+TPU壳*1+硅胶套红蓝色*1+硅胶套黑色*1+钢化膜*2（含清洁包）+PD快充线": "TZ23",
    "Switch2潜水员戴夫收纳包（PU）+2代switch摇杆帽-戴夫+TPU壳*1+硅胶套红蓝色*1+硅胶套黑色*1+钢化膜*2（含清洁包）+PD快充线": "FL-BUNL-NSW-001-DAVE",
    "KS37-黑色": "KS37-4", "KS37-白色": "KS37-05", "KS42-黑色": "KS42-1", "KS42-白色": "KS42-2",
    "KS51-透明黑": "KS51-04T", "KS52-黑色": "KS52-04G", "KS52-粉色": "KS52-03G", "KS52-灰色": "KS52-02G", "KS62-紫色": "KS62-06",
}


# ============ 通用 HTTP ============
def _http(url, data=None, headers=None, method=None, form=False):
    h = {"Content-Type": ("application/x-www-form-urlencoded" if form else "application/json; charset=utf-8")}
    if headers:
        h.update(headers)
    body = (data.encode() if form else json.dumps(data).encode()) if data is not None else None
    last = None
    for a in range(6):
        try:
            r = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if data is not None else "GET"))
            with urllib.request.urlopen(r, timeout=40) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                last = e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
        time.sleep(2 * (a + 1))
    raise last


# ============ 飞书 ============
_FS_TOK = None
def _fs(url, data=None, method=None):
    global _FS_TOK
    if _FS_TOK is None:
        _FS_TOK = _http("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                        {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, None, "POST").get("tenant_access_token")
    for _ in range(3):
        j = _http(url, data, {"Authorization": "Bearer " + _FS_TOK}, method)
        if j.get("code") == 99991663:
            _FS_TOK = _http("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                            {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, None, "POST").get("tenant_access_token")
            time.sleep(1); continue
        return j
    return j


def _sheet(sid, rng):
    u = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{ZL_SS}/values/{sid}!{rng}?valueRenderOption=UnformattedValue"
    return _fs(u, None, "GET").get("data", {}).get("valueRange", {}).get("values", []) or []


def _txt(v):
    if isinstance(v, list):
        return ",".join(_txt(x) for x in v)
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or str(v.get("value", ""))
    return "" if v is None else str(v)


def _num(v, d=0.0):
    try:
        if isinstance(v, list) and v:
            v = v[0]
        if isinstance(v, dict):
            v = v.get("text") or v.get("value")
        return float(v)
    except (TypeError, ValueError):
        return d


# ============ 美通订单 API(头程源) ============
def meitong_orders():
    u = urllib.parse.quote(MEITONG_USER)
    body = (f"client_id={MEITONG_CLIENT_ID}&client_secret={MEITONG_CLIENT_SECRET}&grant_type=password"
            f"&username={u}&password={urllib.parse.quote(MEITONG_PASS)}&login_type=account&user_type=1&type=account")
    tok = _http(MEITONG_BASE + "/v1/user/oauth/token", body, None, "POST", form=True).get("access_token")
    if not tok:
        raise RuntimeError("美通登录失败")
    hdr = {"authorization": "Bearer " + tok, "api-key": "Bearer " + tok}
    out, page = [], 1
    while True:
        d = _http(MEITONG_BASE + "/v1/order/aafOrder/queryOrderByPage",
                  {"orderType": "1", "pageNum": page, "pageSize": 100}, hdr, "POST").get("data", {})
        out.extend(d.get("records", []))
        if page >= d.get("pages", 1) or not d.get("records"):
            break
        page += 1
    return out


# ============ 领星 ERP(name→sku + sku集) ============
def _lx_sign(p):
    # 与本地已验证 meitong_sku_resolver._sign 逐字一致(过滤空值, AES-128-ECB over MD5)
    s = "&".join(f"{k}={p[k]}" for k in sorted(p) if p[k] not in ("", None))
    md5 = hashlib.md5(s.encode()).hexdigest().upper()
    key = LX_ID.encode(); key = key[:16] if len(key) >= 16 else key + b"\x00" * (16 - len(key))
    pad = sym_padding.PKCS7(128).padder(); d = pad.update(md5.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(d) + enc.finalize()).decode()


_LX_TOK = None
def _lx_token():
    global _LX_TOK
    if _LX_TOK:
        return _LX_TOK
    b = "----F" + str(int(time.time()))  # token 必须 multipart form 传 appId/appSecret
    body = (f'--{b}\r\nContent-Disposition: form-data; name="appId"\r\n\r\n{LX_ID}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="appSecret"\r\n\r\n{LX_SECRET}\r\n--{b}--\r\n')
    r = urllib.request.Request("https://openapi.lingxing.com/api/auth-server/oauth/access-token",
                               data=body.encode(), headers={"Content-Type": f"multipart/form-data; boundary={b}"}, method="POST")
    with urllib.request.urlopen(r, timeout=40) as resp:
        _LX_TOK = json.loads(resp.read())["data"]["access_token"]
    return _LX_TOK


def _lx(path, biz):
    tok = _lx_token(); ts = str(int(time.time()))
    common = {"access_token": tok, "app_key": LX_ID, "timestamp": ts}
    sp = {**common}                                  # sign 含 biz 参数
    for k, v in biz.items():
        sp[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in common.items()) + "&sign=" + urllib.parse.quote(_lx_sign(sp))
    r = urllib.request.Request(f"https://openapi.lingxing.com{path}?{qs}",
                               data=json.dumps(biz).encode(), headers={"Content-Type": "application/json"}, method="POST")
    for _ in range(3):
        try:
            with urllib.request.urlopen(r, timeout=40) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(3)
    raise RuntimeError("lx fail " + path)


def load_erp():
    """领星 productList → (name2erp dict, sku_set)。"""
    name2erp, sku_set = {}, set()
    off = 0
    while True:
        r = _lx("/erp/sc/routing/data/local_inventory/productList", {"offset": off, "length": 200})
        data = r.get("data") or []
        for p in data:
            sku = p.get("sku"); nm = (p.get("product_name") or "").strip()
            if sku:
                sku_set.add(sku)
            if nm:
                name2erp.setdefault(nm, sku)
        tot = int(r.get("total") or 0)
        if off + 200 >= tot or not data:
            break
        off += 200
    return name2erp, sku_set


def resolve_erp(key, name2erp, sku_set):
    key = (key or "").strip()
    if not key:
        return None, "empty"
    if key in sku_set:
        return key, "is_erp"
    if key in name2erp:
        return name2erp[key], "name_exact"
    if key in ALIAS_TO_ERP:
        return ALIAS_TO_ERP[key], "alias"
    if key.startswith("X00"):
        return None, "needs_fnsku"
    return None, "unmapped"


# ============ 费率快照 / 海外仓操作费快照 / 指令明细箱数 ============
def load_rate_versions():
    items = _fs(f"https://open.feishu.cn/open-apis/bitable/v1/apps/{SHIP_APP}/tables/{RATE_T}/records/search?page_size=500", {}, "POST").get("data", {}).get("items", [])
    out = {}
    for it in items:
        f = it["fields"]; svc = _txt(f.get("服务类型")); eff_ms = _num(f.get("生效日期"), 0)
        if not svc or not eff_ms:
            continue
        eff = datetime.datetime.fromtimestamp(eff_ms / 1000).strftime("%Y-%m-%d")
        out.setdefault(svc, []).append({"eff": eff, "cbm_rate": _num(f.get("单价"))})
    return out


def rate_for(versions, service, ship_date):
    vers = sorted(versions.get(service, []), key=lambda v: v["eff"], reverse=True)
    for v in vers:
        if ship_date and ship_date >= v["eff"]:
            return v["cbm_rate"]
    return vers[-1]["cbm_rate"] if vers else None


def oversea_box_fee():
    """单箱箱标费(美通最新) = 每箱箱标张数 × 换贴标外箱单价。缺回退6。"""
    items = _fs(f"https://open.feishu.cn/open-apis/bitable/v1/apps/{SHIP_APP}/tables/{OVS_T}/records/search?page_size=500", {}, "POST").get("data", {}).get("items", [])
    best = None
    for it in items:
        f = it["fields"]
        if _txt(f.get("服务商")) != "美通":
            continue
        eff = _num(f.get("生效日期"), 0)
        fee = _num(f.get("每箱箱标张数"), 2) * _num(f.get("换贴标外箱单价"), 3)
        if best is None or eff >= best[0]:
            best = (eff, fee)
    return best[1] if best else 6.0


def zhiling_boxcount():
    """指令明细 sheet → {运单号: SUM(箱数)} = 实际换标箱数。"""
    out = defaultdict(float)
    for r in _sheet(ZL_SHEET_CMD, "A1:M3000")[1:]:
        wb = str(r[12]).strip() if len(r) > 12 and r[12] else ""   # M列 物流货件号
        if not wb.startswith("ZSMX"):
            continue
        try:
            box = float(str(r[11]).strip()) if len(r) > 11 and r[11] not in (None, "") else 0  # L列 箱数
        except ValueError:
            box = 0
        out[wb] += box
    return dict(out)


# ============ 活表 SKU 解析(运单→SKU/数量/平台) ============
def live_resolver():
    by_wb = defaultdict(list)
    for r in _sheet(ZL_SHEET_LIVE, "A1:Q3000")[1:]:
        g = lambda i: (r[i] if len(r) > i else None)
        key = str(g(15) or g(1) or "").strip()                 # 货件号(col15) or 箱唛(col1)
        if not key.startswith("ZSMX"):
            continue
        wb = key.split("U")[0]
        label = str(g(14) or "").strip(); pname = str(g(10) or "").strip()  # 国内所贴产品标签 / 产品名
        sku = label if label.startswith("X00") else (pname or label)
        try:
            qty = float(str(g(11)).strip()) if g(11) not in (None, "") else 0.0
        except ValueError:
            qty = 0.0
        store = str(g(9) or "")
        plat = "美客多" if "美客多" in store else "亚马逊"
        by_wb[wb].append({"sku": sku, "qty": qty, "platform": plat})
    return by_wb


def _cutoff(months):
    if not months:
        return None
    t = datetime.date.today(); m = t.month - months
    y = t.year + (m - 1) // 12; m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}-{min(t.day,28):02d}"


# ============ 摊分: 每 ERP SKU 的 头程/海外仓 per件单价 ============
def build_unit(months=12):
    orders = meitong_orders()
    cut = _cutoff(months)
    if cut:
        orders = [o for o in orders if ((o.get("warehouseArrivalTime") or o.get("orderTime") or o.get("createTime") or "")[:10] >= cut)]
    versions = load_rate_versions()
    box_fee = oversea_box_fee()
    zl = zhiling_boxcount()
    by_wb = live_resolver()
    name2erp, sku_set = load_erp()

    head_by_wb, ovs_by_wb = {}, {}
    for o in orders:
        wb = o.get("orderNo")
        if not wb:
            continue
        try:
            cbm = float(o.get("realChargeableNum") or o.get("realVolume") or 0)
        except (TypeError, ValueError):
            cbm = 0.0
        rate = rate_for(versions, o.get("channelName") or "", (o.get("warehouseArrivalTime") or o.get("orderTime") or o.get("createTime") or "")[:10])
        head_by_wb[wb] = head_by_wb.get(wb, 0.0) + (cbm * rate if (cbm and rate) else 0.0)
    for wb, nbox in zl.items():
        ovs_by_wb[wb] = nbox * box_fee

    agg = defaultdict(lambda: {"head": 0.0, "ovs": 0.0, "qty": 0.0, "plat": ""})
    for wb in set(head_by_wb) | set(ovs_by_wb):
        skus = by_wb.get(wb, [])
        if not skus:
            continue
        tq = sum(s["qty"] for s in skus) or len(skus)
        for s in skus:
            w = (s["qty"] / tq) if tq else (1.0 / len(skus))
            erp_sku, _ = resolve_erp(s["sku"], name2erp, sku_set)
            if not erp_sku:
                continue
            a = agg[erp_sku]
            a["head"] += head_by_wb.get(wb, 0) * w
            a["ovs"] += ovs_by_wb.get(wb, 0) * w
            a["qty"] += s["qty"]; a["plat"] = s["platform"]
    return {k: (v["head"] / v["qty"] if v["qty"] else 0, v["ovs"] / v["qty"] if v["qty"] else 0)
            for k, v in agg.items() if v["plat"] == "美客多"}


# ============ 写 ML 报表 ============
def run(period, months=12, commit=False):
    """灌 period(如 month_2026-04) 的美通中转 头程/海外仓成本。commit=False 只预览。"""
    unit = build_unit(months)
    recs, pt = [], None
    while True:
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{ML_APP}/tables/{ML_T}/records/search?page_size=500" + (f"&page_token={pt}" if pt else "")
        d = _fs(url, {}, "POST").get("data", {})
        recs += d.get("items", [])
        pt = d.get("page_token")
        if not d.get("has_more") or not pt:
            break
    rows = [r for r in recs if _txt(r["fields"].get("周期")) == period]
    written, blank, mh, mo, detail = 0, 0, 0.0, 0.0, []
    for r in rows:
        f = r["fields"]; sku = _txt(f.get("SKU"))
        if sku not in unit:
            blank += 1; continue
        qty = _num(f.get("件数"))
        uh, uo = unit[sku]; hc = round(uh * qty, 2); oc = round(uo * qty, 2)
        mh += hc; mo += oc
        detail.append({"sku": sku, "qty": qty, "head": hc, "ovs": oc})
        if commit:
            _fs(f"https://open.feishu.cn/open-apis/bitable/v1/apps/{ML_APP}/tables/{ML_T}/records/{r['record_id']}",
                {"fields": {F_HEAD: hc, F_OVS: oc}}, "PUT")
        written += 1
    return {"period": period, "months": months, "committed": commit, "rows_in_period": len(rows),
            "meitong_skus": len(unit), "written": written, "blank_non_meitong": blank,
            "head_total": round(mh, 2), "ovs_total": round(mo, 2), "detail": detail}
