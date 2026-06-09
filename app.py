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
        granted.append(fld)
    return {"month": ym, "granted": granted, "finance_members": list(fin.values())}


@app.post("/grant")
async def grant(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    return do_grant()


@app.get("/health")
def health(): return {"ok": True}


@app.post("/audit")
async def audit(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    return do_audit()
