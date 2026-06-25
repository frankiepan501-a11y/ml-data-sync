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
    # 墨客多发货台产品名→ERP(2026-06-22 转单号 join 账单 + ML标题 + 标签 三重确认)
    "Switch2钢化膜3pack": "PPPJ01",   # =NS2-屏幕保护膜(账单)/3pcs Cristal Mica(ML)/标签GXGZ49783
    # 指令明细产品名字→ERP(指令明细无ERP-SKU列, 贴标费靠产品名解析; 头程走gGxKHQ的ERP-SKU列不需此)
    "2代小黑包套装（吊+膜+帽)": "TZ04",   # 巴西三沐(俊辉2026-06-25); ML卖TZ03→lingxing别名TZ03→TZ04
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
    """指令明细 sheet → {运单号: SUM(箱数)} = 实际换标箱数。按表头名取列(防插列错位)。"""
    out = defaultdict(float)
    rows = _sheet(ZL_SHEET_CMD, "A1:M3000")
    if not rows:
        return dict(out)
    hdr = {str(h).strip(): i for i, h in enumerate(rows[0])}
    c_wb = hdr.get("物流货件号", 12); c_box = hdr.get("箱数", 11)
    for r in rows[1:]:
        wb = str(r[c_wb]).strip() if (len(r) > c_wb and r[c_wb]) else ""
        if not wb.startswith("ZSMX"):
            continue
        try:
            box = float(str(r[c_box]).strip()) if (len(r) > c_box and r[c_box] not in (None, "")) else 0
        except ValueError:
            box = 0
        out[wb] += box
    return dict(out)


# ============ 活表 SKU 解析(运单→SKU/数量/平台) ============
def live_resolver():
    # 🚨 按表头名取列, 不硬编索引(2026-06-18 踩坑: 发货台插「ERP-SKU」列致硬编索引全右移1位→
    #   读错列(把"国内所贴产品标签"当货件号)→ ZSMX过滤命中0 → meitong_skus=0 头程/海外仓静默全0)。
    by_wb = defaultdict(list)
    rows = _sheet(ZL_SHEET_LIVE, "A1:Q3000")
    if not rows:
        return by_wb
    hdr = {str(h).strip(): i for i, h in enumerate(rows[0])}
    c_box = hdr.get("送往中转仓箱唛", 1)
    c_store = hdr.get("店铺", 9)
    c_erpcol = hdr.get("ERP-SKU")                              # 物流直填 ERP SKU(最可靠), 见下
    c_pname = hdr.get("产品名", 11)
    c_qty = hdr.get("数量", 12)
    c_label = hdr.get("国内所贴产品标签", 15)
    c_wb = hdr.get("送往中转仓的货件号", 16)
    for r in rows[1:]:
        g = lambda i: (r[i] if (i is not None and len(r) > i) else None)
        key = str(g(c_wb) or g(c_box) or "").strip()           # 货件号 or 箱唛
        if not key.startswith("ZSMX"):
            continue
        wb = key.split("U")[0]
        label = str(g(c_label) or "").strip(); pname = str(g(c_pname) or "").strip()
        erpcol = str(g(c_erpcol) or "").strip()                # 物流填的 ERP-SKU 列优先
        # 解析优先级: ① 物流填的 ERP-SKU 列(直读最可靠, 治本) ② X00 FNSKU 标签 ③ 产品名
        sku = erpcol or (label if label.startswith("X00") else (pname or label))
        try:
            qty = float(str(g(c_qty)).strip()) if g(c_qty) not in (None, "") else 0.0
        except ValueError:
            qty = 0.0
        store = str(g(c_store) or "")
        plat = "美客多" if "美客多" in store else "亚马逊"
        by_wb[wb].append({"sku": sku, "qty": qty, "platform": plat})
    return by_wb


def _cutoff(months):
    if not months:
        return None
    t = datetime.date.today(); m = t.month - months
    y = t.year + (m - 1) // 12; m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}-{min(t.day,28):02d}"


def _serial_date(v):
    """gGxKHQ 日期列多为 Excel 序列号(如 45839=2025-07-02) → 'YYYY-MM-DD'。"""
    try:
        n = int(float(v))
        if n > 20000:
            return (datetime.date(1899, 12, 30) + datetime.timedelta(days=n)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    s = str(v or "")[:10].replace("/", "-").replace(".", "-")
    return s if s.startswith("20") else ""


# ============ 墨客多头程(gGxKHQ 运费列, 账单验证一致, 无需 API) ============
def mokeduo_by_trans(cut=None):
    """gGxKHQ 物流商=墨客多 行 → {转单号: {fee, date, items:[{sku,qty,platform}]}}。
    🔑 墨客多「运费」列已是头程真值(2026-06-22 用墨客多对账单 STA00325070500012 逐运单验证一致, 费率2850/CBM);
       运费仅记在转单号首箱行, 数量是每箱 → 按转单号 Σ每箱数量; 同美通靠转单号 join 拿真 SKU(账单品名是报关名不可用)。"""
    rows = _sheet(ZL_SHEET_LIVE, "A1:Z3000")
    out = {}
    if not rows:
        return out
    hdr = {str(h).strip(): i for i, h in enumerate(rows[0])}
    c_carrier = hdr.get("物流商", 5); c_ship = hdr.get("实际发货时间", 2)
    c_store = hdr.get("店铺", 9); c_erp = hdr.get("ERP-SKU"); c_pname = hdr.get("产品名", 11)
    c_qty = hdr.get("数量", 12); c_label = hdr.get("国内所贴产品标签", 15)
    c_wb = hdr.get("送往中转仓的货件号", 16); c_fee = hdr.get("运费", 22)
    cur = None
    for r in rows[1:]:
        g = lambda i: (r[i] if (i is not None and len(r) > i) else None)
        carrier = str(g(c_carrier) or "").strip()
        if carrier:
            cur = carrier
        if cur != "墨客多":
            continue
        wbfull = str(g(c_wb) or "").strip()
        if not wbfull:
            continue
        trans = wbfull.split("/")[0]
        d = out.setdefault(trans, {"fee": 0.0, "date": "", "items": []})
        try:
            d["fee"] += float(g(c_fee)) if g(c_fee) not in (None, "") else 0.0
        except (TypeError, ValueError):
            pass
        if not d["date"]:
            d["date"] = _serial_date(g(c_ship))
        label = str(g(c_label) or "").strip(); pname = str(g(c_pname) or "").strip()
        erpcol = str(g(c_erp) or "").strip()
        sku = erpcol or (label if label.startswith("X00") else (pname or label))
        try:
            qty = float(g(c_qty)) if g(c_qty) not in (None, "") else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        store = str(g(c_store) or ""); plat = "美客多" if "美客多" in store else "亚马逊"
        if sku and qty:
            d["items"].append({"sku": sku, "qty": qty, "platform": plat})
    if cut:
        out = {k: v for k, v in out.items() if (not v["date"]) or v["date"] >= cut}
    return out


def _plat_of(store, country=""):
    """平台归属。🚨 区分 美客多-墨西哥(美通/墨客多) vs 美客多-巴西(三沐): 巴西另套三沐成本。"""
    s = str(store or ""); c = str(country or "")
    if "巴西" in s or "巴西" in c or "三沐" in s or "AIRSOFT" in s.upper():
        return "美客多巴西"
    if "美客多" in s:
        return "美客多"
    return "亚马逊"


# ============ 通用 Z 列(单个产品头程): 物流手工算好的 per件头程, 直读 ============
def zcol_rows(cut=None):
    """gGxKHQ 任何货代行, 若「单个产品头程」(Z列) 已填 → 直接用作 per件头程(俊辉为三沐/万国手工算的,
    建议 AI 直接抓 Z 列)。head_row = Z × 数量。美通/墨客多 Z 列空(走各自逻辑)→不进此 pass 不双算。"""
    rows = _sheet(ZL_SHEET_LIVE, "A1:AD3000")
    out = []
    if not rows:
        return out
    hdr = {str(h).strip(): i for i, h in enumerate(rows[0])}
    c_z = hdr.get("单个产品头程")
    if c_z is None:
        return out
    c_qty = hdr.get("数量", 12); c_store = hdr.get("店铺", 9); c_country = hdr.get("国家", 7)
    c_erp = hdr.get("ERP-SKU"); c_pname = hdr.get("产品名", 11); c_label = hdr.get("国内所贴产品标签", 15)
    c_ship = hdr.get("实际发货时间", 2)
    for r in rows[1:]:
        g = lambda i: (r[i] if (i is not None and len(r) > i) else None)
        try:
            z = float(g(c_z)) if g(c_z) not in (None, "") else 0.0
        except (TypeError, ValueError):
            z = 0.0
        try:
            qty = float(g(c_qty)) if g(c_qty) not in (None, "") else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        if z <= 0 or qty <= 0:
            continue
        if cut:
            sd = _serial_date(g(c_ship))
            if sd and sd < cut:
                continue
        label = str(g(c_label) or "").strip(); pname = str(g(c_pname) or "").strip()
        erpcol = str(g(c_erp) or "").strip()
        sku = erpcol or (label if label.startswith("X00") else (pname or label))
        out.append({"sku": sku, "qty": qty, "z": z, "platform": _plat_of(g(c_store), g(c_country))})
    return out


def sanmu_ovs_rows(cut=None):
    """指令明细(72g5oE) 巴西三沐行「产品标费用/RMB」(俊辉公式: 换标的按3元/产品贴标费) → 海外仓 ovs。
    俊辉口径: 巴西三沐海外仓仅扣产品标签贴标费。只取 美客多巴西(美通/墨客多墨西哥海外仓走 zhiling_boxcount 箱费)。"""
    rows = _sheet(ZL_SHEET_CMD, "A1:AB3000")
    out = []
    if not rows:
        return out
    hdr = {str(h).strip(): i for i, h in enumerate(rows[0])}
    c_fee = hdr.get("产品标费用/RMB")
    if c_fee is None:
        return out
    c_qty = hdr.get("数量"); c_pname = hdr.get("产品名字"); c_label = hdr.get("送仓标签")
    c_store = hdr.get("店铺"); c_country = hdr.get("国家"); c_ship = hdr.get("到仓时间")
    for r in rows[1:]:
        g = lambda i: (r[i] if (i is not None and len(r) > i) else None)
        try:
            fee = float(g(c_fee)) if g(c_fee) not in (None, "") else 0.0
        except (TypeError, ValueError):
            fee = 0.0
        if fee <= 0:
            continue
        plat = _plat_of(g(c_store), g(c_country))
        if plat != "美客多巴西":            # 只巴西三沐贴标费; 墨西哥海外仓走 zhiling_boxcount
            continue
        try:
            qty = float(g(c_qty)) if g(c_qty) not in (None, "") else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        if cut:
            sd = _serial_date(g(c_ship))
            if sd and sd < cut:
                continue
        label = str(g(c_label) or "").strip(); pname = str(g(c_pname) or "").strip()
        sku = label if label.startswith("X00") else (pname or label)
        out.append({"sku": sku, "qty": qty, "ovs": fee, "platform": plat})
    return out


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

    # 🚨 按 (ERP SKU, 平台) 聚合, 不混平台。原 last-write-wins(a["plat"]=s["platform"]) bug:
    #   同 SKU 发到美客多+亚马逊两店时, 保留与否取决于 set 迭代顺序(非确定) → meitong_skus 15↔35 漂移,
    #   且美客多单价被亚马逊发货量稀释。改为 per-平台隔离 → 美客多成本只用美客多发货, 确定且准。
    agg = defaultdict(lambda: {"head": 0.0, "ovs": 0.0, "qty": 0.0})
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
            a = agg[(erp_sku, s["platform"])]
            a["head"] += head_by_wb.get(wb, 0) * w
            a["ovs"] += ovs_by_wb.get(wb, 0) * w
            a["qty"] += s["qty"]

    # 🟡 墨客多: gGxKHQ 物流商=墨客多 行(运费列=头程真值), 转单号 Σ每箱数量, 运费按数量份额拆到 SKU,
    #   混进同一 agg → per件单价自动按 carrier(美通+墨客多) 加权混合, 同 SKU 跨货代不双算。
    #   墨客多运费列只含运费不含海外仓(报关350/票同美通口径不计) → 不加 ovs。
    for trans, t in mokeduo_by_trans(cut).items():
        tq = sum(it["qty"] for it in t["items"])
        if tq <= 0:
            continue
        for it in t["items"]:
            erp_sku, _ = resolve_erp(it["sku"], name2erp, sku_set)
            if not erp_sku:
                continue
            a = agg[(erp_sku, it["platform"])]
            a["head"] += t["fee"] * (it["qty"] / tq)
            a["qty"] += it["qty"]

    # 🟠 通用 Z 列(单个产品头程): 三沐(巴西)/万国 等物流手工算好的 per件头程, head=Z×数量 混进 agg。
    #   三沐巴西店→"美客多巴西"平台(run() 把巴西 ML 行路由到此, 不与墨西哥美通/墨客多混)。
    #   🚨 不套 12mo cutoff: Z 是物流算好的 per件值(与时间无关, 非滚动均价); 且三沐巴西发货稀疏周转慢
    #   (2025/4 发的货 2026/5 才卖), 套窗口会把老批次排除→TZ02/TZ03 等漏覆盖。
    for it in zcol_rows(None):
        erp_sku, _ = resolve_erp(it["sku"], name2erp, sku_set)
        if not erp_sku:
            continue
        a = agg[(erp_sku, it["platform"])]
        a["head"] += it["z"] * it["qty"]
        a["qty"] += it["qty"]

    # 🟠 巴西三沐 海外仓 = 产品标签贴标费(指令明细 产品标费用/RMB, 俊辉算 3元/产品换标的)。
    #   单独 agg(自己的 qty), 不混进上面 head 的 qty(否则稀释 head per件)。
    sovs = defaultdict(lambda: {"ovs": 0.0, "qty": 0.0})
    for it in sanmu_ovs_rows(None):   # 同 Z-pass: 贴标费 per件与时间无关, 不套 cutoff
        erp_sku, _ = resolve_erp(it["sku"], name2erp, sku_set)
        if not erp_sku:
            continue
        s = sovs[(erp_sku, it["platform"])]
        s["ovs"] += it["ovs"]; s["qty"] += it["qty"]

    # 返回 (ERP SKU, 平台) → per件(头程, 海外仓); 含 美客多(墨西哥) + 美客多巴西(三沐)。
    #   海外仓 per件 = 美通/墨客多箱费(v.ovs/v.qty, 墨西哥) + 巴西三沐贴标费(sovs, 各按自己 qty), 平台不重叠不双算。
    out = {}
    for (erp, plat), v in agg.items():
        if plat not in ("美客多", "美客多巴西") or not v["qty"]:
            continue
        ovs_u = v["ovs"] / v["qty"]
        so = sovs.get((erp, plat))
        if so and so["qty"]:
            ovs_u += so["ovs"] / so["qty"]
        out[(erp, plat)] = (v["head"] / v["qty"], ovs_u)
    return out


# ============ 诊断(只读, 不写报表) ============
def diag(period="month_2026-05", months=12):
    """全链路解析诊断: 每个发货台产品名/标签 → key_used → resolve_erp 结果, 平台分布,
    订单cbm填充, unit dict, 与 ML 期间行 SKU 的交集缺口。"""
    orders = meitong_orders()
    cut = _cutoff(months)
    zsmx = [o for o in orders if str(o.get("orderNo", "")).startswith("ZSMX")]
    in_win = [o for o in zsmx if ((o.get("warehouseArrivalTime") or o.get("orderTime") or o.get("createTime") or "")[:10] >= (cut or "0"))]
    cbm_ok = sum(1 for o in zsmx if (o.get("realChargeableNum") or o.get("realVolume")))
    name2erp, sku_set = load_erp()

    # 重跑 live_resolver 但保留原始字段
    rows = _sheet(ZL_SHEET_LIVE, "A1:Q3000")
    hdr = {str(h).strip(): i for i, h in enumerate(rows[0])} if rows else {}
    cb = hdr.get("送往中转仓箱唛", 1); cs = hdr.get("店铺", 9); cp = hdr.get("产品名", 11)
    cq = hdr.get("数量", 12); cl = hdr.get("国内所贴产品标签", 15); cw = hdr.get("送往中转仓的货件号", 16)
    cerp = hdr.get("ERP-SKU", 10)
    win_wb = set(o.get("orderNo") for o in in_win)
    resmap = {}
    for r in rows[1:] if rows else []:
        g = lambda i: (r[i] if (i is not None and len(r) > i) else None)
        key = str(g(cw) or g(cb) or "").strip()
        if not key.startswith("ZSMX"):
            continue
        wb = key.split("U")[0]
        label = str(g(cl) or "").strip(); pname = str(g(cp) or "").strip(); erpcol = str(g(cerp) or "").strip()
        sku_key = label if label.startswith("X00") else (pname or label)
        store = str(g(cs) or ""); plat = "美客多" if "美客多" in store else "亚马逊"
        try:
            qty = float(str(g(cq)).strip()) if g(cq) not in (None, "") else 0.0
        except ValueError:
            qty = 0.0
        erp, reason = resolve_erp(sku_key, name2erp, sku_set)
        k = (pname, label, store)
        d = resmap.setdefault(k, {"pname": pname, "label": label, "erpcol": erpcol, "store": store,
                                  "plat": plat, "key_used": sku_key, "erp": erp, "reason": reason,
                                  "qty": 0.0, "wbs": set(), "wbs_in_win": set()})
        d["qty"] += qty; d["wbs"].add(wb)
        if wb in win_wb:
            d["wbs_in_win"].add(wb)
    res = []
    for d in resmap.values():
        d["n_wb"] = len(d["wbs"]); d["n_wb_in_win"] = len(d["wbs_in_win"])
        del d["wbs"], d["wbs_in_win"]
        res.append(d)
    res.sort(key=lambda x: -x["qty"])

    unit = build_unit(months)
    # ML 期间行
    recs, pt = [], None
    while True:
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{ML_APP}/tables/{ML_T}/records/search?page_size=500" + (f"&page_token={pt}" if pt else "")
        dd = _fs(url, {}, "POST").get("data", {})
        recs += dd.get("items", []); pt = dd.get("page_token")
        if not dd.get("has_more") or not pt:
            break
    ml = []
    for r in recs:
        f = r["fields"]
        if _txt(f.get("周期")) != period:
            continue
        sku = _txt(f.get("SKU")); store = _txt(f.get("店铺"))
        tp = "美客多巴西" if ("巴西" in store or "AIRSOFT" in store.upper()) else "美客多"
        ml.append({"sku": sku, "qty": _num(f.get("件数")), "in_unit": (sku, tp) in unit,
                   "seller": store, "target_plat": tp})
    return {"period": period, "months": months, "cutoff": cut,
            "orders_total": len(orders), "zsmx": len(zsmx), "zsmx_in_window": len(in_win),
            "zsmx_cbm_ok": cbm_ok,
            "live_resolution": res,
            "unit": {f"{e}|{p}": {"head_u": round(v[0], 4), "ovs_u": round(v[1], 4)} for (e, p), v in unit.items()},
            "ml_rows": ml,
            "ml_in_unit": [m["sku"] for m in ml if m["in_unit"]],
            "ml_not_in_unit": [m["sku"] for m in ml if not m["in_unit"]]}


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
    # ML 后台 seller_sku → 领星 ERP SKU(俊辉确认的 CBT 定制 listing 别名, 如 MXCFFLFFSCP-TOTK→FF01A-04),
    # 与主 sync 同源, 否则 unit 按 ERP 键、ML 行按 seller_sku 键, 永远匹配不上。
    try:
        from app.lingxing import resolve_erp_sku as _ml_alias
    except Exception:
        _ml_alias = lambda s: s
    written, blank, mh, mo, detail = 0, 0, 0.0, 0.0, []
    for r in rows:
        f = r["fields"]
        # 🚨 按店铺路由平台: 墨西哥美客多(CBT/本土)→"美客多"(美通+墨客多); 巴西店(AIRSOFT)→"美客多巴西"(三沐)。
        #   防 FB07-7/KS35-19 等同 SKU 跨国把墨西哥成本误算到巴西(反之亦然)。
        store = _txt(f.get("店铺"))
        target_plat = "美客多巴西" if ("巴西" in store or "AIRSOFT" in store.upper()) else "美客多"
        sku = _ml_alias(_txt(f.get("SKU")))
        key = (sku, target_plat)
        if key not in unit:
            blank += 1; continue
        qty = _num(f.get("件数"))
        uh, uo = unit[key]; hc = round(uh * qty, 2); oc = round(uo * qty, 2)
        mh += hc; mo += oc
        detail.append({"sku": sku, "qty": qty, "head": hc, "ovs": oc})
        if commit:
            _fs(f"https://open.feishu.cn/open-apis/bitable/v1/apps/{ML_APP}/tables/{ML_T}/records/{r['record_id']}",
                {"fields": {F_HEAD: hc, F_OVS: oc}}, "PUT")
        written += 1
    return {"period": period, "months": months, "committed": commit, "rows_in_period": len(rows),
            "meitong_skus": len(unit), "written": written, "blank_non_meitong": blank,
            "head_total": round(mh, 2), "ovs_total": round(mo, 2), "detail": detail}
