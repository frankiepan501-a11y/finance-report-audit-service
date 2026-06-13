# -*- coding: utf-8 -*-
"""全渠道毛利报表月度审计 (Zeabur)。每月9号16:00 n8n cron → POST /audit。
检查: 数据缺漏(空报表) / 采购成本覆盖(有销售但cg=0) / 物流头程覆盖。
异常 → 飞书卡片发财务部 + Frankie, 列 渠道/店铺/负责人/异常, 让财务跟运营核实。
口径: 只 flag「销售额>0 且 成本=0」(真异常); 销售=0的0成本行忽略。"""
import os, json, datetime
from collections import defaultdict
import requests
from fastapi import FastAPI, Request, HTTPException

APP_ID = os.environ["FEISHU_APP_ID"]      # 聪哥1号
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
FEISHU = "https://open.feishu.cn/open-apis"
app = FastAPI()

IDX_APP = "P9awbhG9faFstxsO1KZc9b9Qnxb"; IDX_TBL = "tblrProDcHtwD5Vr"  # 公司毛利报表索引
ML_APP = "WM3LbBr76aRqMys2of8c1dGInEb"; ML_TBL = "tbl09sRPkX35PDfU"   # 美客多报表
FIN = {"吴晓丹": "ou_c65fc5c31c650790db623640b7ac74f7",
       "莫莉莉": "ou_73b0e93529a1dab9509274aa756d1064",
       "林纯子": "ou_eaf3d06fc7f7691352aab69c9e75baee"}
FRANKIE = "ou_629ce01f4bc31de078e10fcb038dbf78"
# 跨境报表名 → 字段名(索引表) 映射(取链接拿token)
XB_FIELDS = ["亚马逊毛利报表", "沃尔玛毛利报表", "速卖通毛利报表", "TikTok Shop毛利报表",
             "独立站毛利报表", "独立站Powkong Admin API毛利报表"]


def tok():
    r = requests.post(f"{FEISHU}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=20)
    return r.json()["tenant_access_token"]


def num(x):
    try: return float(str(x).replace(",", ""))
    except: return None


def ft(v):
    if isinstance(v, list) and v:
        x = v[0]; return x.get("text", "") if isinstance(x, dict) else str(x)
    if isinstance(v, dict): return v.get("text") or v.get("link") or v.get("value") or ""
    return v if v is not None else ""


def colidx(hdr, *keys):
    for i, h in enumerate(hdr):
        if h and all(k in h for k in keys): return i
    return None


def _idx_row(T, ym):
    """取索引表 ym(2026/05) 行的各报表链接。"""
    items = []; pt = None
    while True:
        u = f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{IDX_TBL}/records?page_size=500" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers={"Authorization": f"Bearer {T}"}, timeout=30).json().get("data", {})
        items += d.get("items") or []; pt = d.get("page_token")
        if not d.get("has_more"): break
    for r in items:
        f = r["fields"]
        if ft(f.get("日期")) == ym:
            return {k: (f.get(k, {}).get("link") if isinstance(f.get(k), dict) else "") for k in XB_FIELDS}
    return {}


def _sheet_token(url):
    if not url or "/sheets/" not in url: return None
    return url.split("/sheets/")[1].split("?")[0].split("#")[0]


def audit_xb(T, name, ss):
    """审一个跨境 sheet 报表 → findings。"""
    H = {"Authorization": f"Bearer {T}"}
    sh = requests.get(f"{FEISHU}/sheets/v3/spreadsheets/{ss}/sheets/query", headers=H, timeout=30).json()
    sheets = sh.get("data", {}).get("sheets", [])
    if not sheets: return [[name, "-", "-", "空报表/数据缺漏", f"{name} 报表无 sheet", 0]]
    sid = sorted(sheets, key=lambda s: -(s.get("grid_properties", {}).get("row_count") or 0))[0]["sheet_id"]
    r = requests.get(f"{FEISHU}/sheets/v2/spreadsheets/{ss}/values/{sid}!A1:BZ900?valueRenderOption=ToString", headers=H, timeout=40).json()
    vals = r.get("data", {}).get("valueRange", {}).get("values") or []
    hdr = vals[0] if vals else []
    rows = [v for v in vals[1:] if any(c not in (None, "") for c in v)]
    if not rows or not any(hdr):
        return [[name, "-", "-", "空报表/数据缺漏", f"{name} 报表 0 行无数据", 0]]
    ci = dict(sku=colidx(hdr, "MSKU"), nm=colidx(hdr, "中文名称"), shop=colidx(hdr, "店铺"),
              own=colidx(hdr, "负责人"), cost=colidx(hdr, "采购成本", "RMB"))
    ci_sales = colidx(hdr, "销售额", "RMB") or colidx(hdr, "售价", "RMB")
    out = []
    for row in rows:
        def g(i): return row[i] if i is not None and i < len(row) else ""
        sales = num(g(ci_sales)) or 0; cost = num(g(ci["cost"]))
        if sales > 0 and (cost == 0 or cost is None):
            out.append([name, g(ci["shop"]), g(ci["own"]), "采购成本=0(有销售)", f"{g(ci['sku'])} {g(ci['nm'])}", round(sales)])
    return out


def audit_ml(T):
    H = {"Authorization": f"Bearer {T}"}
    items = []; pt = None
    while True:
        u = f"{FEISHU}/bitable/v1/apps/{ML_APP}/tables/{ML_TBL}/records?page_size=500" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers=H, timeout=30).json().get("data", {})
        items += d.get("items") or []; pt = d.get("page_token")
        if not d.get("has_more"): break
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    ymd = last.strftime("%Y-%m")
    out = []
    for r in items:
        f = r["fields"]
        if ymd not in ft(f.get("周期")): continue
        rev = num(ft(f.get("营收(RMB)"))) or 0; cost = num(ft(f.get("采购成本(RMB)")))
        if rev > 0 and (cost == 0 or cost is None):
            out.append(["美客多", ft(f.get("店铺")), "梁俊辉", "采购成本=0(有销售)", f"{ft(f.get('SKU'))} {ft(f.get('商品标题'))[:30]}", round(rev)])
    return out


# ===== 自愈: 发现成本缺口 → 自动触发该渠道重新同步(采购补成本后下次审计自动修复) =====
ML_SYNC_URL = os.environ.get("ML_SYNC_URL", "https://ml-sync.zeabur.app")
ML_SYNC_TOKEN = os.environ.get("ML_SYNC_TOKEN", "")
# 美客多 店铺名关键词 → seller_id (重同步只更新 bitable, 不发通知, 安全)
ML_SELLER = {"巴西": "2378517428", "FUNLABDIRECTMX": "1407362838", "FUNLAB_MX": "1436420028",
             "VALMIGOZ": "3383185411", "CBT": "1502520822"}


def _ml_seller(shop):
    for kw, sid in ML_SELLER.items():
        if kw in (shop or ""): return sid
    return None


def self_heal(groups, ym_dash):
    """对有成本缺口的渠道触发重新同步。v1 只 ML(安全:bitable更新无广播)。
    b国内电商/跨境重跑会广播给多人, 暂不自动触发(只告警)。"""
    healed = []; fired = set()
    for (ch, shop, own), g in groups.items():
        if ch == "美客多" and ML_SYNC_TOKEN:
            sid = _ml_seller(shop)
            if sid and sid not in fired:
                fired.add(sid)
                try:  # fire-and-forget: 网关2m切但服务端继续跑完
                    requests.post(f"{ML_SYNC_URL}/report/sync-feishu-monthly?seller_id={sid}&month={ym_dash}",
                                  headers={"Authorization": f"Bearer {ML_SYNC_TOKEN}"}, timeout=6)
                except Exception:
                    pass  # 预期超时(fire-and-forget)
                healed.append(f"美客多·{shop}")
    return healed


def build_card(ym, groups, empties, healed=None):
    healed = healed or []
    els = [{"tag": "div", "text": {"tag": "lark_md", "content": f"本月毛利报表自动审计（{ym}）发现以下异常，请财务部跟对应运营负责人核实："}}]
    if not groups and not empties:
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": "✅ 全渠道无异常：采购成本、物流头程覆盖均正常。"}})
    for (ch, shop, own), (n, amt, skus) in sorted(groups.items(), key=lambda x: -x[1][1]):
        els.append({"tag": "hr"})
        skutxt = " / ".join(skus[:12])
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**采购成本缺失（毛利虚高）**\n**渠道**：{ch}　**店铺**：{shop}　**负责人**：{own or '待确认'}\n**异常**：{n} 个 SKU 有销售但领星成本=0，涉及金额 **¥{amt:.0f}**\n`{skutxt}`"}})
    for ch in empties:
        els.append({"tag": "hr"})
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**数据缺漏（报表空）**\n**渠道**：{ch}　**异常**：{ym} 报表 0 行无数据 → 请运营确认是否有销售/补传/重新生成"}})
    if healed:
        els.append({"tag": "hr"})
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": "🔧 **已自动触发重新同步**：" + " / ".join(healed) + "\n（若领星已有成本则约 5 分钟内自动修复；若仍 0 = 采购尚未在领星补成本，请采购补后下次审计自动修）"}})
    els.append({"tag": "hr"})
    els.append({"tag": "div", "text": {"tag": "lark_md", "content": "📌 审计维度：数据缺漏 / 采购成本覆盖 / 物流头程覆盖。请财务部核实后跟运营负责人推进修复。"}})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "orange", "title": {"tag": "plain_text", "content": f"🟠 [FIN·P1] 全渠道毛利报表审计 · {ym}"}},
            "elements": els}


def do_audit():
    T = tok()
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    ym_slash = last.strftime("%Y/%m"); ym_dash = last.strftime("%Y-%m")
    links = _idx_row(T, ym_slash)
    findings = []
    name_map = {"亚马逊毛利报表": "亚马逊", "沃尔玛毛利报表": "沃尔玛", "速卖通毛利报表": "速卖通",
                "TikTok Shop毛利报表": "TikTok", "独立站毛利报表": "独立站", "独立站Powkong Admin API毛利报表": "独立站Powkong"}
    for fld, url in links.items():
        ss = _sheet_token(url)
        if not ss:
            findings.append([name_map.get(fld, fld), "-", "-", "空报表/数据缺漏", f"{name_map.get(fld, fld)} {ym_slash} 报表链接缺失", 0]); continue
        try: findings += audit_xb(T, name_map.get(fld, fld), ss)
        except Exception as e: findings.append([name_map.get(fld, fld), "-", "-", "审计异常", str(e)[:80], 0])
    try: findings += audit_ml(T)
    except Exception: pass
    groups = defaultdict(lambda: [0, 0.0, []]); empties = []
    for x in findings:
        if "空报表" in x[3] or "缺失" in x[3] and "报表" in x[4]:
            empties.append(x[0]); continue
        if x[3].startswith("采购成本=0"):
            k = (x[0], x[1], x[2]); g = groups[k]; g[0] += 1; g[1] += x[5]; g[2].append(x[4].split()[0])
    empties = sorted(set(empties))
    healed = self_heal(groups, ym_dash)
    card = build_card(ym_dash, groups, empties, healed)
    sent = []
    for nm, oid in {**FIN, "Frankie": FRANKIE}.items():
        try:
            r = requests.post(f"{FEISHU}/im/v1/messages?receive_id_type=open_id",
                              headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                              json={"receive_id": oid, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}, timeout=20).json()
            sent.append(f"{nm}:{r.get('code')}")
        except Exception as e: sent.append(f"{nm}:err")
    return {"month": ym_dash, "anomaly_groups": len(groups), "empty_reports": empties, "healed": healed, "sent": sent}


# ===== 自动授权: 月报生成后给 财务部全体+Frankie+吴晓丹 授权(铁律①) =====
FIN_DEPT = "od-ad59abe171a6b0a419a5e3969fb349ad"  # 财务部(实时解析成员, 新人自动包含)
WXD = "ou_c65fc5c31c650790db623640b7ac74f7"        # 吴晓丹
# 索引表所有报表字段 → 授权(国内线下=数据app不在此列, 单独权限)
GRANT_FIELDS = XB_FIELDS + ["美客多毛利报表", "国内电商毛利报表"]


def _dept_members(T, did):
    res = {}; pt = None
    while True:
        u = f"{FEISHU}/contact/v3/users?department_id={did}&page_size=50&user_id_type=open_id&department_id_type=open_department_id" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers={"Authorization": f"Bearer {T}"}, timeout=20).json().get("data", {})
        for u2 in d.get("items", []): res[u2["open_id"]] = u2.get("name")
        if d.get("has_more"): pt = d["page_token"]
        else: break
    return res


# ===== 渠道负责人按职务实时解析(铁律①第3类: 对应渠道运营负责人也自动获权) =====
DEPT_XB = "od-a69452a48133671d028ac82491c65a9f"   # 跨境电商平台部(亚马逊/美客多)
DEPT_ZW = "od-5fdbfdf97a0f9c1305c42f39fb729125"   # 站外运营部(TikTok/独立站)
DEPT_GN = "od-2e75af50a81b16d829e8b345f9137a49"   # 国内电商平台部
# 渠道报表字段 → (部门, 职务关键词)。按职务实时查 → 人员入离/调岗自动跟随。
# 沃尔玛/速卖通 暂无专职运营 → 不映射(财务部+Frankie+吴晓丹仍获权), 待 Frankie 指定后补。
CHANNEL_OWNER = {
    "亚马逊毛利报表": (DEPT_XB, "亚马逊运营"),
    "美客多毛利报表": (DEPT_XB, "美客多运营"),
    "TikTok Shop毛利报表": (DEPT_ZW, "TK运营"),
    "独立站毛利报表": (DEPT_ZW, "独立站运营"),
    "独立站Powkong Admin API毛利报表": (DEPT_ZW, "独立站运营"),
    "国内电商毛利报表": (DEPT_GN, "国内平台运营"),
}
# 沃尔玛/速卖通 无专职运营职务 → 按 Frankie 指定的人固定绑(2026-06-09)。
# 沃尔玛=林明坚(亚马逊运营专员兼) / 速卖通=赵伟俊(国内平台运营专员兼)
CHANNEL_OWNER_FIXED = {
    "沃尔玛毛利报表": ["ou_35aa6883c0598bac5c7e06fcb06f7c4d"],   # 林明坚
    "速卖通毛利报表": ["ou_274ee5199a763b7ec97980cd54e3fecb"],   # 赵伟俊
}
_dept_jt_cache = {}


def _dept_users_jt(T, did):
    """部门成员 → [(open_id, job_title)]。请求级缓存(do_grant 开头清), 保持职务实时。"""
    if did in _dept_jt_cache: return _dept_jt_cache[did]
    res = []; pt = None
    while True:
        u = f"{FEISHU}/contact/v3/users?department_id={did}&page_size=50&user_id_type=open_id&department_id_type=open_department_id" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers={"Authorization": f"Bearer {T}"}, timeout=20).json().get("data", {})
        for u2 in d.get("items", []): res.append((u2["open_id"], u2.get("job_title") or ""))
        if d.get("has_more"): pt = d["page_token"]
        else: break
    _dept_jt_cache[did] = res
    return res


def _owners_for(T, fld):
    if fld in CHANNEL_OWNER_FIXED:
        return CHANNEL_OWNER_FIXED[fld]
    m = CHANNEL_OWNER.get(fld)
    if not m: return []
    did, kw = m
    return [oid for oid, jt in _dept_users_jt(T, did) if kw in jt]


def _parse_link(url):
    if not url: return None, None
    if "/sheets/" in url: return url.split("/sheets/")[1].split("?")[0].split("#")[0], "sheet"
    if "/base/" in url: return url.split("/base/")[1].split("?")[0].split("#")[0], "bitable"
    return None, None


def _grant_one(T, token, typ, oid, perm):
    try:
        r = requests.post(f"{FEISHU}/drive/v1/permissions/{token}/members?type={typ}&need_notification=false",
                          headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                          json={"member_type": "openid", "member_id": oid, "perm": perm}, timeout=20).json()
        return r.get("code")
    except Exception:
        return -1


def _idx_links_all(T, ym_slash):
    items = []; pt = None
    while True:
        u = f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{IDX_TBL}/records?page_size=500" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers={"Authorization": f"Bearer {T}"}, timeout=30).json().get("data", {})
        items += d.get("items") or []; pt = d.get("page_token")
        if not d.get("has_more"): break
    for r in items:
        f = r["fields"]
        if ft(f.get("日期")) == ym_slash:
            return {k: (f.get(k, {}).get("link") if isinstance(f.get(k), dict) else "") for k in GRANT_FIELDS}
    return {}


def do_grant():
    T = tok()
    _dept_jt_cache.clear()  # 每次 /grant 重新拉部门成员 → 职务实时
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    ym = last.strftime("%Y/%m")
    links = _idx_links_all(T, ym)
    fin = _dept_members(T, FIN_DEPT)  # 财务部全体(实时)
    granted = []
    for fld, url in links.items():
        token, typ = _parse_link(url)
        if not token: continue
        _grant_one(T, token, typ, FRANKIE, "full_access")
        _grant_one(T, token, typ, WXD, "edit")
        for oid in fin:
            if oid in (FRANKIE, WXD): continue
            _grant_one(T, token, typ, oid, "view")
        owners = _owners_for(T, fld)  # 渠道负责人(按职务实时查)
        for oid in owners:
            if oid in (FRANKIE, WXD) or oid in fin: continue
            _grant_one(T, token, typ, oid, "view")
        granted.append({"report": fld, "owners": len(owners)})
    return {"month": ym, "granted": granted, "finance_members": list(fin.values())}


@app.post("/grant")
async def grant(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    return do_grant()


# ===================== 全渠道灌总表汇总器 (/aggregate) =====================
# 读各子报表汇总数 → 每渠道聚合一行 upsert 进总表 tbltFK8vwdcrlfBa(FIN App)。
# 幂等(月份+渠道大类+平台+店铺)。速卖通/线下c/B2Bd 由各自服务自灌不在此; TikTok 暂不做。
TOTAL_TBL = "tbltFK8vwdcrlfBa"   # 全渠道销售总览(在 IDX_APP=P9aw...)
# (索引字段, 渠道大类, 平台[须总表已有选项/空], 店铺label, 品牌[或空], parser)
AGG_REPORTS = [
    ("亚马逊毛利报表", "跨境电商", "亚马逊", "亚马逊全站汇总", "", "xb"),
    ("沃尔玛毛利报表", "跨境电商", "沃尔玛", "沃尔玛全站汇总", "", "xb"),
    ("独立站毛利报表", "跨境电商", "独立站", "funlab.net Shopify(FUNLAB)", "FUNLAB", "xb"),
    ("独立站Powkong Admin API毛利报表", "跨境电商", "独立站", "powkong.com Shopify(POWKONG)", "POWKONG", "xb"),
    ("美客多毛利报表", "跨境电商", "美客多", "美客多5店汇总", "", "ml"),
    ("国内电商毛利报表", "国内电商", "", "9平台11店汇总", "", "ecom"),
]


def _aggnum(x):
    try: return float(str(x).replace(",", "").replace("¥", "").replace("%", ""))
    except: return 0.0


def colexact(hdr, name):
    for i, h in enumerate(hdr):
        if h == name: return i
    return None


def _bitable_all(T, app, tbl):
    out = []; pt = None
    while True:
        u = f"{FEISHU}/bitable/v1/apps/{app}/tables/{tbl}/records?page_size=500" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers={"Authorization": f"Bearer {T}"}, timeout=30).json().get("data", {})
        out += d.get("items") or []; pt = d.get("page_token")
        if not d.get("has_more"): break
    return out


def _sheet_vals(T, ss, prefer=None):
    H = {"Authorization": f"Bearer {T}"}
    sh = requests.get(f"{FEISHU}/sheets/v3/spreadsheets/{ss}/sheets/query", headers=H, timeout=30).json()
    shs = sh.get("data", {}).get("sheets", []) or []
    if not shs: return [], None
    sid = title = None
    if prefer:
        for s in shs:
            if prefer in (s.get("title") or ""): sid, title = s["sheet_id"], s.get("title"); break
    if not sid:
        s = sorted(shs, key=lambda s: -((s.get("grid_properties", {}) or {}).get("row_count") or 0))[0]
        sid, title = s["sheet_id"], s.get("title")
    r = requests.get(f"{FEISHU}/sheets/v2/spreadsheets/{ss}/values/{sid}!A1:CZ1000?valueRenderOption=UnformattedValue", headers=H, timeout=60).json()
    return r.get("data", {}).get("valueRange", {}).get("values") or [], title


def _agg_xb(T, ss):
    vals, title = _sheet_vals(T, ss)
    if not vals: return None
    hdr = vals[0]; rows = [v for v in vals[1:] if any(c not in (None, "") for c in v)]
    c = dict(sales=colidx(hdr, "售价", "RMB") or colidx(hdr, "销售额", "RMB"),
             margin=colidx(hdr, "毛利润", "RMB") or colidx(hdr, "毛利", "RMB"),
             payback=colidx(hdr, "回款", "RMB"), cost=colidx(hdr, "采购成本", "RMB"),
             freight=colidx(hdr, "头程", "RMB"), ad1=colidx(hdr, "广告费", "RMB"), ad2=colidx(hdr, "推广费", "RMB"),
             commission=colidx(hdr, "佣金", "RMB"), deliver=colidx(hdr, "配送费", "RMB"),
             storage=colidx(hdr, "仓储费", "RMB"), vat=colidx(hdr, "VAT", "RMB"), adj=colidx(hdr, "调整", "RMB"),
             qty=colexact(hdr, "销量"), rq=colidx(hdr, "退货数量") or colidx(hdr, "退款数量"))
    a = dict(sales=0, margin=0, payback=0, cost=0, freight=0, ad=0, pf=0, qty=0, rq=0)
    def g(row, i): return _aggnum(row[i]) if (i is not None and i < len(row)) else 0
    for row in rows:
        a["sales"] += g(row, c["sales"]); a["margin"] += g(row, c["margin"]); a["payback"] += g(row, c["payback"])
        a["cost"] += g(row, c["cost"]); a["freight"] += g(row, c["freight"]); a["ad"] += g(row, c["ad1"]) + g(row, c["ad2"])
        a["pf"] += g(row, c["commission"]) + g(row, c["deliver"]) + g(row, c["storage"]) + g(row, c["vat"]) + g(row, c["adj"])
        a["qty"] += g(row, c["qty"]); a["rq"] += g(row, c["rq"])
    for k in ("cost", "freight", "ad", "pf"): a[k] = abs(a[k])
    return a


def _agg_ecom(T, ss):
    vals, title = _sheet_vals(T, ss, prefer="毛利结果")
    if not vals: return None
    hdr = vals[0]; rows = vals[1:]
    c = dict(sales=colexact(hdr, "销售额"), margin=colexact(hdr, "毛利额"), pf=colexact(hdr, "平台费合计"),
             ad=colexact(hdr, "推广/广告费"), cost=colidx(hdr, "采购成本"), freight=colexact(hdr, "物流成本"),
             qty=colexact(hdr, "销量"), rq=colexact(hdr, "退款数量"))
    a = dict(sales=0, margin=0, payback=0, cost=0, freight=0, ad=0, pf=0, qty=0, rq=0)
    def g(row, i): return _aggnum(row[i]) if (i is not None and i < len(row)) else 0
    for row in rows:
        if not any(x not in (None, "") for x in row): continue
        if g(row, c["sales"]) == 0 and g(row, c["margin"]) == 0: continue
        a["sales"] += g(row, c["sales"]); a["margin"] += g(row, c["margin"]); a["cost"] += g(row, c["cost"])
        a["freight"] += g(row, c["freight"]); a["ad"] += g(row, c["ad"]); a["pf"] += g(row, c["pf"])
        a["qty"] += g(row, c["qty"]); a["rq"] += g(row, c["rq"])
    return a


def _agg_ml(T, ym_dash):
    a = dict(sales=0, margin=0, payback=0, cost=0, freight=0, ad=0, pf=0, qty=0, rq=0)
    for r in _bitable_all(T, ML_APP, ML_TBL):
        f = r["fields"]
        if ym_dash not in ft(f.get("周期")): continue
        a["sales"] += _aggnum(ft(f.get("营收(RMB)"))); a["margin"] += _aggnum(ft(f.get("全额毛利(RMB)")))
        a["cost"] += _aggnum(ft(f.get("采购成本(RMB)")))
        a["freight"] += _aggnum(ft(f.get("物流费(RMB)"))) + _aggnum(ft(f.get("头程成本(RMB)"))) + _aggnum(ft(f.get("海外仓成本(RMB)")))
        a["ad"] += _aggnum(ft(f.get("广告费(RMB)"))); a["pf"] += _aggnum(ft(f.get("ML佣金(RMB)"))) + _aggnum(ft(f.get("VAT估算(RMB)")))
        a["qty"] += _aggnum(ft(f.get("销量")))
    for k in ("cost", "freight", "ad", "pf"): a[k] = abs(a[k])
    return a


def _ensure_payback_cols(T):
    have = set(); pt = None
    while True:
        u = f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{TOTAL_TBL}/fields?page_size=100" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers={"Authorization": f"Bearer {T}"}, timeout=30).json().get("data", {})
        for f in d.get("items") or []: have.add(f["field_name"])
        pt = d.get("page_token")
        if not d.get("has_more"): break
    for nm, fmt in (("回款RMB", "0"), ("回款率", "0.00%")):
        if nm not in have:
            requests.post(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{TOTAL_TBL}/fields",
                          headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                          json={"field_name": nm, "type": 2, "property": {"formatter": fmt}}, timeout=20)


def _agg_fields(ym_dash, cat, plat, shop, brand, a, url, ptype):
    mr = a["margin"] / a["sales"] if a["sales"] else 0
    f = {"月份": ym_dash, "渠道大类": cat, "店铺": shop, "销量": round(a["qty"]) or None, "退款数量": round(a["rq"]) or None,
         "销售额RMB": round(a["sales"], 2), "采购成本RMB": round(a["cost"], 2) or None, "物流费RMB": round(a["freight"], 2) or None,
         "广告费RMB": round(a["ad"], 2) or None, "平台费RMB": round(a["pf"], 2) or None, "全额毛利RMB": round(a["margin"], 2),
         "毛利率": round(mr, 4), "源报表链接": {"link": url, "text": f"{plat or cat}-{ym_dash}"}}
    if plat: f["平台"] = plat
    if brand: f["品牌"] = brand
    if ptype == "xb" and a.get("payback"):   # 回款仅跨境(领星给); 美客多/国内电商口径待财务→空
        f["回款RMB"] = round(a["payback"], 2)
        f["回款率"] = round(a["payback"] / a["sales"], 4) if a["sales"] else None
    return {k: v for k, v in f.items() if v not in (None, "")}


def _agg_upsert(T, ym_dash, cat, plat, shop, fields):
    H = {"Authorization": f"Bearer {T}", "Content-Type": "application/json"}
    target = None
    for r in _bitable_all(T, IDX_APP, TOTAL_TBL):
        ff = r["fields"]
        if ft(ff.get("月份")) == ym_dash and ft(ff.get("渠道大类")) == cat and ft(ff.get("平台")) == (plat or "") and ft(ff.get("店铺")) == shop:
            target = r["record_id"]; break
    if target:
        requests.put(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{TOTAL_TBL}/records/{target}", headers=H, json={"fields": fields}, timeout=20)
        return "update"
    requests.post(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{TOTAL_TBL}/records", headers=H, json={"fields": fields}, timeout=20)
    return "create"


def do_aggregate():
    T = tok()
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    ym_slash = last.strftime("%Y/%m"); ym_dash = last.strftime("%Y-%m")
    links = {}
    for r in _bitable_all(T, IDX_APP, IDX_TBL):
        f = r["fields"]
        if ft(f.get("日期")) == ym_slash:
            for k, v in f.items():
                if isinstance(v, dict) and v.get("link"): links[k] = v["link"]
            break
    _ensure_payback_cols(T)
    done = []; skipped = []; errs = []
    for fld, cat, plat, shop, brand, ptype in AGG_REPORTS:
        url = links.get(fld, "")
        if not url:
            skipped.append({"shop": shop, "why": "索引无链接"}); continue
        try:
            if ptype == "xb": a = _agg_xb(T, url.split("/sheets/")[1].split("?")[0])
            elif ptype == "ecom": a = _agg_ecom(T, url.split("/sheets/")[1].split("?")[0])
            else: a = _agg_ml(T, ym_dash)
        except Exception as e:
            errs.append({"shop": shop, "err": str(e)[:100]}); continue
        if not a or a["sales"] <= 0:
            skipped.append({"shop": shop, "why": "销售=0(报表未抓到/无单)", "margin": a["margin"] if a else None}); continue
        fields = _agg_fields(ym_dash, cat, plat, shop, brand, a, url, ptype)
        act = _agg_upsert(T, ym_dash, cat, plat, shop, fields)
        done.append({"shop": shop, "act": act, "sales": round(a["sales"]), "margin": round(a["margin"]), "payback": round(a["payback"])})
    # 总表授权(铁律①): 财务部全体 view + Frankie full + 吴晓丹 edit
    fin = _dept_members(T, FIN_DEPT)
    _grant_one(T, IDX_APP, "bitable", FRANKIE, "full_access")
    _grant_one(T, IDX_APP, "bitable", WXD, "edit")
    for oid in fin:
        if oid not in (FRANKIE, WXD): _grant_one(T, IDX_APP, "bitable", oid, "view")
    return {"month": ym_dash, "loaded": done, "skipped": skipped, "errors": errs, "finance_granted": list(fin.values())}


@app.post("/aggregate")
async def aggregate(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    return do_aggregate()


@app.get("/health")
def health(): return {"ok": True}


@app.post("/audit")
async def audit(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    return do_audit()
