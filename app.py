# -*- coding: utf-8 -*-
"""全渠道毛利报表月度审计 (Zeabur)。每月9号16:00 n8n cron → POST /audit。
检查: 数据缺漏(空报表) / 采购成本覆盖(有销售但cg=0) / 物流头程覆盖。
异常 → 飞书卡片发财务部 + Frankie, 列 渠道/店铺/负责人/异常, 让财务跟运营核实。
口径: 只 flag「销售额>0 且 成本=0」(真异常); 销售=0的0成本行忽略。"""
import os, json, datetime, hashlib, time
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
FRANKIE_UNION_ID = os.environ.get("FRANKIE_UNION_ID", "on_6e85dd60606f76f2d5af892785ac1dfe")
EVENT_APP_ID = os.environ.get("FEISHU_EVENT_APP_ID", APP_ID)       # 聪哥3号: card sender/callback owner
EVENT_APP_SECRET = os.environ.get("FEISHU_EVENT_APP_SECRET", "")
if not EVENT_APP_SECRET:
    EVENT_APP_ID, EVENT_APP_SECRET = APP_ID, APP_SECRET
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://finance-report-audit.zeabur.app")
# 跨境报表名 → 字段名(索引表) 映射(取链接拿token)
XB_FIELDS = ["亚马逊毛利报表", "沃尔玛毛利报表", "速卖通毛利报表", "TikTok Shop毛利报表",
             "独立站毛利报表", "独立站Powkong Admin API毛利报表"]

# 公司级毛利卡片化 ledger: Base 只做系统账本, 运营/财务通过飞书卡片反馈。
COMPANY_RUN_TBL = os.environ.get("COMPANY_PROFIT_RUN_TABLE_ID", "tblR2Ft4aN0a6ARh")
COMPANY_GAP_TBL = os.environ.get("COMPANY_PROFIT_GAP_TABLE_ID", "tblvJaWomx25pAr3")
COMPANY_AUDIT_TBL = os.environ.get("COMPANY_PROFIT_AUDIT_TABLE_ID", "tblVan2P6bsGb9em")
COMPANY_CARD_SCHEMA = "company_profit_card_v1"

COMPANY_PLATFORM_REGISTRY = {
    "amazon": {"name": "Amazon", "platform": "亚马逊", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部"},
    "walmart": {"name": "Walmart", "platform": "沃尔玛", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部"},
    "mercadolibre": {"name": "Mercado Libre", "platform": "美客多", "data_mode": "hybrid", "data_status": "数据已就绪", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部"},
    "funlab_net": {"name": "funlab.net", "platform": "funlab.net", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部"},
    "powkong": {"name": "Powkong", "platform": "Powkong", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部"},
    "domestic_ecom": {"name": "国内电商", "platform": "国内电商", "data_mode": "manual", "data_status": "资料已提交", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部"},
    "funlabswitch": {"name": "funlabswitch", "platform": "funlabswitch", "data_mode": "hybrid", "data_status": "待成本物流维护", "report_status": "P0待处理", "blocker_type": "master_data_gap", "blocker": "采购/负责人"},
    "aliexpress": {"name": "AliExpress", "platform": "速卖通", "data_mode": "api", "data_status": "取数完成", "report_status": "待接统一终审", "blocker_type": "workflow_gap", "blocker": "AI自动化"},
    "tiktok_shop": {"name": "TikTok Shop", "platform": "TikTok Shop", "data_mode": "api", "data_status": "取数完成", "report_status": "待接统一终审", "blocker_type": "workflow_gap", "blocker": "AI自动化"},
    "b2b": {"name": "B2B", "platform": "B2B", "data_mode": "ledger", "data_status": "台账已就绪", "report_status": "待接台账模式", "blocker_type": "workflow_gap", "blocker": "AI自动化"},
    "offline": {"name": "国内线下", "platform": "国内线下", "data_mode": "ledger", "data_status": "台账已就绪", "report_status": "待接台账模式", "blocker_type": "workflow_gap", "blocker": "AI自动化"},
    "temu": {"name": "TEMU", "platform": "TEMU", "data_mode": "manual", "data_status": "待资料提交", "report_status": "待定口径", "blocker_type": "finance_rule_gap", "blocker": "财务/负责人"},
    "taobao": {"name": "淘宝", "platform": "淘宝", "data_mode": "manual", "data_status": "待资料提交", "report_status": "待定口径", "blocker_type": "finance_rule_gap", "blocker": "财务/负责人"},
    "pinduoduo": {"name": "拼多多", "platform": "拼多多", "data_mode": "manual", "data_status": "待资料提交", "report_status": "待定口径", "blocker_type": "finance_rule_gap", "blocker": "财务/负责人"},
}


def _tok_for(app_id, app_secret):
    r = requests.post(f"{FEISHU}/auth/v3/tenant_access_token/internal",
                      json={"app_id": app_id, "app_secret": app_secret}, timeout=20)
    return r.json()["tenant_access_token"]


def tok():
    return _tok_for(APP_ID, APP_SECRET)


def event_tok():
    return _tok_for(EVENT_APP_ID, EVENT_APP_SECRET)


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


# ===== 头程/海外仓覆盖审计(2026-06-18): 美客多有销量但 头程+海外仓全=0 → 待核实货代 =====
# 场景: 墨客多/三沐等货代未接入(头程缺口) 或 该SKU走CBT自发货/直邮(无头程属正常)。
# 只做"提醒"不挂起灌总表(头程缺失不像采购成本缺失那样让毛利严重失真)。
FREIGHT_MIN_REV = float(os.environ.get("ML_FREIGHT_AUDIT_MIN_REV", "1000"))   # 营收阈值,小单不报(降噪)
FREIGHT_EXCLUDE = set(s.strip() for s in os.environ.get("ML_FREIGHT_AUDIT_EXCLUDE", "").split(",") if s.strip())  # 俊辉确认的自发货/直邮SKU免报


def audit_ml_freight(T):
    """美客多: 营收≥阈值 且 头程成本=0 且 海外仓成本=0 → 头程/海外仓缺失(待核实货代)。
    排除: 营收<阈值的小单 + ML_FREIGHT_AUDIT_EXCLUDE 列出的已确认自发货/直邮 SKU。"""
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
        rev = num(ft(f.get("营收(RMB)"))) or 0
        head = num(ft(f.get("头程成本(RMB)"))) or 0
        ovs = num(ft(f.get("海外仓成本(RMB)"))) or 0
        sku = ft(f.get("SKU"))
        if rev >= FREIGHT_MIN_REV and head == 0 and ovs == 0 and sku not in FREIGHT_EXCLUDE:
            out.append(["美客多", ft(f.get("店铺")), "梁俊辉", "头程海外仓=0(有销售待核实货代)", f"{sku} {ft(f.get('商品标题'))[:30]}", round(rev)])
    return out


# ===== 审计卡扩展: 国内电商/速卖通/c国内线下 成本缺失(2026-06-15) =====
SMT_UP_TBL = "tbl5Hvrty3oqLdIF"                              # 速卖通月度数据上传台(在 IDX_APP)
APP_C = "JqZwbSi7uaDlw0sjEFPcTDlenMf"; O_C = "tblJ7Z9cUGTz8fsu"  # c国内线下订单台
ECOM_OWNER = "赵伟俊"; OFFLINE_OWNER = "马建威"


def audit_ecom(T, ss):
    """国内电商: 读 10_毛利结果表, 销售>0 且 采购成本=0 → 成本缺失。"""
    if not ss: return []
    H = {"Authorization": f"Bearer {T}"}
    sh = requests.get(f"{FEISHU}/sheets/v3/spreadsheets/{ss}/sheets/query", headers=H, timeout=30).json()
    sheets = sh.get("data", {}).get("sheets", []) or []
    sid = next((s["sheet_id"] for s in sheets if "毛利结果" in (s.get("title") or "")), None)
    if not sid: return []
    r = requests.get(f"{FEISHU}/sheets/v2/spreadsheets/{ss}/values/{sid}!A1:CZ500?valueRenderOption=UnformattedValue", headers=H, timeout=40).json()
    vals = r.get("data", {}).get("valueRange", {}).get("values") or []
    if not vals: return []
    hdr = vals[0]
    ci_s = colexact(hdr, "销售额"); ci_c = colidx(hdr, "采购成本"); ci_sku = colexact(hdr, "ERP_SKU(=商家编码)") or colidx(hdr, "商家编码")
    ci_nm = colexact(hdr, "标准产品名称"); ci_shop = colexact(hdr, "店铺")
    ci_nq = colexact(hdr, "净销量"); ci_ns = colexact(hdr, "净销售额")
    out = []
    for row in vals[1:]:
        def g(i): return row[i] if i is not None and i < len(row) else ""
        nq = num(g(ci_nq)) or 0; c = num(g(ci_c))
        if nq > 0 and (c == 0 or c is None):   # 净销量>0才算真缺失(排除全退款单 净销=0)
            out.append(["国内电商", g(ci_shop), ECOM_OWNER, "采购成本=0(有净销售)", f"{g(ci_sku) or '(无SKU)'} {str(g(ci_nm))[:20]}", round(num(g(ci_ns)))])
    return out


def audit_offline(T):
    """c国内线下: 订单台当月 买断/赠样 有产品但 单位成本=0(对照表查不到) → 成本缺失。"""
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    ym = last.strftime("%Y-%m"); out = []
    for r in _bitable_all(T, APP_C, O_C):
        f = r["fields"]; d = ft(f.get("下单/出货日期"))
        if not str(d).isdigit(): continue
        if datetime.datetime.utcfromtimestamp(int(d) / 1000).strftime("%Y-%m") != ym: continue
        way = ft(f.get("合作方式")); prod = ft(f.get("产品名")); cg = num(ft(f.get("单位成本(自动)")))
        if way in ("经销买断", "赠样") and prod and cg == 0:
            out.append(["国内线下", ft(f.get("关联经销商")), OFFLINE_OWNER, "采购成本=0(对照表查不到)", prod[:24], round(num(ft(f.get("订单金额"))))])
    return out


def audit_smt(T):
    """速卖通: 读上传台当月记录摘要, 含「领星缺cg」→ 成本缺失(复用smt已算输出)。"""
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    ym = last.strftime("%Y-%m"); out = []
    for r in _bitable_all(T, IDX_APP, SMT_UP_TBL):
        f = r["fields"]
        if ym not in ft(f.get("月份")): continue
        summ = ft(f.get("计算结果摘要"))
        if "缺cg" in summ:
            seg = summ.split("缺cg", 1)[1][:60]
            out.append(["速卖通", "FUNLAB+LinYuvo", ECOM_OWNER, "采购成本=0(领星缺cg)", f"领星缺cg{seg}", 0])
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


def build_card(ym, groups, empties, healed=None, freight_groups=None):
    healed = healed or []; freight_groups = freight_groups or {}
    els = [{"tag": "div", "text": {"tag": "lark_md", "content": f"本月毛利报表自动审计（{ym}）发现以下异常，请财务部跟对应运营负责人核实："}}]
    if not groups and not empties and not freight_groups:
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": "✅ 全渠道无异常：采购成本、头程/海外仓覆盖均正常。"}})
    for (ch, shop, own), (n, amt, skus) in sorted(groups.items(), key=lambda x: -x[1][1]):
        els.append({"tag": "hr"})
        skutxt = " / ".join(skus[:12])
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**采购成本缺失（毛利虚高）**\n**渠道**：{ch}　**店铺**：{shop}　**负责人**：{own or '待确认'}\n**异常**：{n} 个 SKU 有销售但领星成本=0，涉及金额 **¥{amt:.0f}**\n`{skutxt}`"}})
    for (ch, shop, own), (n, amt, skus) in sorted(freight_groups.items(), key=lambda x: -x[1][1]):
        els.append({"tag": "hr"})
        skutxt = " / ".join(skus[:12])
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**头程/海外仓缺失（毛利虚高，待核实货代）**\n**渠道**：{ch}　**店铺**：{shop}　**负责人**：{own or '待确认'}\n**异常**：{n} 个 SKU 有销售但头程+海外仓成本=0，涉及营收 **¥{amt:.0f}**\n`{skutxt}`\n（应走货代中转→补对接/填发货台ERP-SKU；若CBT自发货/直邮无头程属正常→俊辉确认后加进 ML_FREIGHT_AUDIT_EXCLUDE 免报）"}})
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
    try: findings += audit_ml_freight(T)   # 头程/海外仓覆盖(2026-06-18)
    except Exception: pass
    try: findings += audit_ecom(T, _sheet_token(_idx_links_all(T, ym_slash).get("国内电商毛利报表", "")))  # 国内电商
    except Exception: pass
    try: findings += audit_offline(T)   # c国内线下
    except Exception: pass
    try: findings += audit_smt(T)       # 速卖通
    except Exception: pass
    groups = defaultdict(lambda: [0, 0.0, []]); freight_groups = defaultdict(lambda: [0, 0.0, []]); empties = []
    for x in findings:
        if "空报表" in x[3] or "缺失" in x[3] and "报表" in x[4]:
            empties.append(x[0]); continue
        if x[3].startswith("采购成本=0"):
            k = (x[0], x[1], x[2]); g = groups[k]; g[0] += 1; g[1] += x[5]; g[2].append(x[4].split()[0])
        elif x[3].startswith("头程海外仓=0"):
            k = (x[0], x[1], x[2]); g = freight_groups[k]; g[0] += 1; g[1] += x[5]; g[2].append(x[4].split()[0])
    empties = sorted(set(empties))
    healed = self_heal(groups, ym_dash)   # 仅采购成本缺口自愈(重同步); 头程缺口需接货代,不自愈
    card = build_card(ym_dash, groups, empties, healed, freight_groups)
    sent = []
    for nm, oid in {**FIN, "Frankie": FRANKIE}.items():
        try:
            r = requests.post(f"{FEISHU}/im/v1/messages?receive_id_type=open_id",
                              headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                              json={"receive_id": oid, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}, timeout=20).json()
            sent.append(f"{nm}:{r.get('code')}")
        except Exception as e: sent.append(f"{nm}:err")
    return {"month": ym_dash, "anomaly_groups": len(groups), "freight_groups": len(freight_groups), "empty_reports": empties, "healed": healed, "sent": sent}


# ===== 自动授权: 月报生成后给 财务部全体+Frankie+吴晓丹 授权(铁律①) =====
FIN_DEPT = "od-ad59abe171a6b0a419a5e3969fb349ad"  # 财务部(实时解析成员, 新人自动包含)
WXD = "ou_c65fc5c31c650790db623640b7ac74f7"        # 吴晓丹
# 索引表所有报表字段 → 授权(国内线下=数据app不在此列, 单独权限)
GRANT_FIELDS = XB_FIELDS + ["美客多毛利报表", "国内电商毛利报表",
                            "独立站funlab.net毛利报表", "独立站funlabswitch毛利报表", "TEMU毛利报表"]


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
    "独立站funlab.net毛利报表": (DEPT_ZW, "独立站运营"),
    "独立站funlabswitch毛利报表": (DEPT_ZW, "独立站运营"),
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
# 审计 gate 阈值: 成本缺失金额占渠道销售 > 此% → 挂起不灌(Frankie 2026-06-13 定5%;空报表/0销售永远挂起)
GATE_PCT = float(os.environ.get("GATE_COST_MISSING_PCT", "5"))
# (索引字段, 渠道大类, 平台[须总表已有选项/空], 店铺label, 品牌[或空], parser)
AGG_REPORTS = [
    ("亚马逊毛利报表", "跨境电商", "亚马逊", "亚马逊全站汇总", "", "xb"),
    ("沃尔玛毛利报表", "跨境电商", "沃尔玛", "沃尔玛全站汇总", "", "xb"),
    ("独立站毛利报表", "跨境电商", "独立站", "funlab.net Shopify(FUNLAB)", "FUNLAB", "xb"),
    ("独立站Powkong Admin API毛利报表", "跨境电商", "独立站", "powkong.com Shopify(POWKONG)", "POWKONG", "xb"),
    ("独立站funlabswitch毛利报表", "跨境电商", "独立站", "funlabswitch.com Shopline(FUNLAB)", "FUNLAB", "xb"),  # Shopline大站, sheet schema同xb, 回款列[34]已算(销售-网关费); 替代原手动灌表脚本
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


# ===================== 公司级毛利报表卡片化 ledger / callback =====================
def _now_ms():
    return int(time.time() * 1000)


def _compact_json(value, limit=9000):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit - 20] + "...[truncated]"


def _payload_hash(value):
    return hashlib.sha256(_compact_json(value, 20000).encode("utf-8")).hexdigest()


def _company_platform(platform_id):
    key = (platform_id or "funlabswitch").strip()
    if key not in COMPANY_PLATFORM_REGISTRY:
        raise HTTPException(400, f"unknown platform: {key}")
    return key, COMPANY_PLATFORM_REGISTRY[key]


def _company_run_id(period, platform_id):
    return f"company-profit-{period}-{platform_id}"


def _company_card_id(card_type, run_id, target_id=""):
    return hashlib.sha1(f"{card_type}:{run_id}:{target_id}".encode("utf-8")).hexdigest()[:12]


def _company_idem(action, run_id, card_id, target_id, nonce):
    raw = f"{COMPANY_CARD_SCHEMA}:{action}:{run_id}:{card_id}:{target_id}:{nonce}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _bt_find(T, tbl, field, value):
    for r in _bitable_all(T, IDX_APP, tbl):
        if ft(r.get("fields", {}).get(field)) == value:
            return r
    return None


def _bt_write(T, tbl, fields, key_field=None):
    H = {"Authorization": f"Bearer {T}", "Content-Type": "application/json"}
    target = _bt_find(T, tbl, key_field, fields.get(key_field)) if key_field and fields.get(key_field) else None
    if target:
        requests.put(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{tbl}/records/{target['record_id']}",
                     headers=H, json={"fields": fields}, timeout=20)
        return {"act": "update", "record_id": target["record_id"]}
    r = requests.post(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{tbl}/records",
                      headers=H, json={"fields": fields}, timeout=20).json()
    return {"act": "create", "record_id": (r.get("data") or {}).get("record", {}).get("record_id")}


def _company_seed_run(T, period, platform_id, *, override=None):
    platform_id, meta = _company_platform(platform_id)
    run_id = _company_run_id(period, platform_id)
    fields = {
        "run_id": run_id,
        "期间": period,
        "平台": meta["platform"],
        "data_mode": meta["data_mode"],
        "数据状态": meta["data_status"],
        "报表状态": meta["report_status"],
        "缺口责任类型": meta.get("blocker_type", ""),
        "当前阻断方": meta.get("blocker", ""),
        "P0数量": "1" if meta.get("report_status") == "P0待处理" else "0",
        "总表状态": "待灌总表" if meta.get("report_status") == "财务通过" else "未灌",
        "最后动作": "seed_run",
        "最后动作时间": str(_now_ms()),
        "payload_json": _compact_json({"platform_id": platform_id, "meta": meta}),
    }
    if override:
        fields.update({k: str(v) if isinstance(v, (int, float, bool)) else v for k, v in override.items()})
    _bt_write(T, COMPANY_RUN_TBL, fields, "run_id")
    return _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {"fields": fields}


def _company_create_gap(T, run_id, period, platform, gap_type, detail, *, p_level="P0", owner=""):
    gap_id = "gap_" + hashlib.sha1(f"{run_id}:{gap_type}:{detail}".encode("utf-8")).hexdigest()[:14]
    fields = {
        "gap_id": gap_id,
        "run_id": run_id,
        "期间": period,
        "平台": platform,
        "缺口责任类型": gap_type,
        "P级": p_level,
        "缺口说明": detail,
        "证据": detail,
        "责任人": owner,
        "处理结果": "待处理",
        "是否可进财务终审": "false",
        "最后动作": "create_gap",
        "最后动作时间": str(_now_ms()),
        "payload_json": _compact_json({"run_id": run_id, "gap_type": gap_type, "detail": detail}),
    }
    _bt_write(T, COMPANY_GAP_TBL, fields, "gap_id")
    return _bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id) or {"fields": fields}


def _company_update_run(T, run_id, fields):
    fields = dict(fields)
    fields["最后动作时间"] = str(_now_ms())
    rec = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)
    if not rec:
        return None
    H = {"Authorization": f"Bearer {T}", "Content-Type": "application/json"}
    requests.put(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{COMPANY_RUN_TBL}/records/{rec['record_id']}",
                 headers=H, json={"fields": fields}, timeout=20)
    return _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)


def _company_update_gap(T, gap_id, fields):
    fields = dict(fields)
    fields["最后动作时间"] = str(_now_ms())
    rec = _bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id)
    if not rec:
        return None
    H = {"Authorization": f"Bearer {T}", "Content-Type": "application/json"}
    requests.put(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{COMPANY_GAP_TBL}/records/{rec['record_id']}",
                 headers=H, json={"fields": fields}, timeout=20)
    return _bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id)


def _company_audit_exists(T, idempotency_key):
    return bool(_bt_find(T, COMPANY_AUDIT_TBL, "idempotency_key", idempotency_key))


def _company_write_audit(T, idempotency_key, action, actor_open_id, run_id, target_type, target_id,
                         before, after, payload, result, source_message_id=""):
    if _company_audit_exists(T, idempotency_key):
        return
    _bt_write(T, COMPANY_AUDIT_TBL, {
        "idempotency_key": idempotency_key,
        "action": action,
        "actor_open_id": actor_open_id,
        "run_id": run_id,
        "target_type": target_type,
        "target_id": target_id,
        "before_json": _compact_json(before),
        "after_json": _compact_json(after),
        "payload_hash": _payload_hash(payload),
        "result": result,
        "source_message_id": source_message_id,
        "created_at": str(_now_ms()),
        "payload_json": _compact_json(payload),
    }, "idempotency_key")


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
    a = dict(sales=0, margin=0, payback=0, cost=0, freight=0, ad=0, pf=0, qty=0, rq=0, cm_n=0, cm_amt=0)
    def g(row, i): return _aggnum(row[i]) if (i is not None and i < len(row)) else 0
    for row in rows:
        rs = g(row, c["sales"]); rc = g(row, c["cost"])
        if rs > 0 and rc == 0: a["cm_n"] += 1; a["cm_amt"] += rs   # 成本缺失行(销售>0且采购=0)
        a["sales"] += rs; a["margin"] += g(row, c["margin"]); a["payback"] += g(row, c["payback"])
        a["cost"] += rc; a["freight"] += g(row, c["freight"]); a["ad"] += g(row, c["ad1"]) + g(row, c["ad2"])
        a["pf"] += g(row, c["commission"]) + g(row, c["deliver"]) + g(row, c["storage"]) + g(row, c["vat"]) + g(row, c["adj"])
        a["qty"] += g(row, c["qty"]); a["rq"] += g(row, c["rq"])
    for k in ("cost", "freight", "ad", "pf"): a[k] = abs(a[k])
    return a


def _agg_ecom(T, ss):
    vals, title = _sheet_vals(T, ss, prefer="毛利结果")
    if not vals: return None
    hdr = vals[0]; rows = vals[1:]
    c = dict(sales=colexact(hdr, "销售额") or colidx(hdr, "销售额"), margin=colexact(hdr, "毛利额"), pf=colexact(hdr, "平台费合计"),
             ad=colexact(hdr, "推广/广告费"), cost=colidx(hdr, "采购成本"), freight=colexact(hdr, "物流成本"),
             qty=colexact(hdr, "销量"), rq=colexact(hdr, "退款数量") or colidx(hdr, "退款数量"),
             netqty=colexact(hdr, "净销量"), netsales=colexact(hdr, "净销售额"))
    a = dict(sales=0, margin=0, payback=0, cost=0, freight=0, ad=0, pf=0, qty=0, rq=0, cm_n=0, cm_amt=0)
    def g(row, i): return _aggnum(row[i]) if (i is not None and i < len(row)) else 0
    for row in rows:
        if not any(x not in (None, "") for x in row): continue
        rs = g(row, c["sales"])
        if rs == 0 and g(row, c["margin"]) == 0: continue
        # 成本缺失=有"净成交"却无采购(用净销量排除全退款单: 毛销>0但净销=0 是退款非缺失)
        if g(row, c["netqty"]) > 0 and g(row, c["cost"]) == 0: a["cm_n"] += 1; a["cm_amt"] += g(row, c["netsales"])
        a["sales"] += rs; a["margin"] += g(row, c["margin"]); a["cost"] += g(row, c["cost"])
        a["freight"] += g(row, c["freight"]); a["ad"] += g(row, c["ad"]); a["pf"] += g(row, c["pf"])
        a["qty"] += g(row, c["qty"]); a["rq"] += g(row, c["rq"])
        a["netsales"] = a.get("netsales", 0) + g(row, c["netsales"])   # 回款用(净销售额)
    return a


def _agg_ml(T, ym_dash):
    a = dict(sales=0, margin=0, payback=0, cost=0, freight=0, ad=0, pf=0, qty=0, rq=0, cm_n=0, cm_amt=0)
    for r in _bitable_all(T, ML_APP, ML_TBL):
        f = r["fields"]
        if ym_dash not in ft(f.get("周期")): continue
        rs = _aggnum(ft(f.get("营收(RMB)"))); rc = _aggnum(ft(f.get("采购成本(RMB)")))
        if rs > 0 and rc == 0: a["cm_n"] += 1; a["cm_amt"] += rs
        a["sales"] += rs; a["margin"] += _aggnum(ft(f.get("全额毛利(RMB)")))
        a["cost"] += rc
        a["freight"] += _aggnum(ft(f.get("物流费(RMB)"))) + _aggnum(ft(f.get("头程成本(RMB)"))) + _aggnum(ft(f.get("海外仓成本(RMB)")))
        a["ad"] += _aggnum(ft(f.get("广告费(RMB)"))); a["pf"] += _aggnum(ft(f.get("ML佣金(RMB)"))) + _aggnum(ft(f.get("VAT估算(RMB)")))
        a["qty"] += _aggnum(ft(f.get("销量")))
        # 回款(实际到账近似 A): ml-sync 未抓 Mercado Pago settlement → Frankie 口径 fallback = 营收 − ML佣金 − 退款
        a["payback"] += rs - _aggnum(ft(f.get("ML佣金(RMB)"))) - _aggnum(ft(f.get("退款金额(RMB)")))
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
    if ptype == "xb" and a.get("payback"):   # 跨境回款=领星放款金额(实际到账 A)
        f["回款RMB"] = round(a["payback"], 2)
        f["回款率"] = round(a["payback"] / a["sales"], 4) if a["sales"] else None
    if ptype == "ecom" and a.get("sales"):   # 国内电商回款=净销售额−平台费合计(平台全额结算货款,无reserve,B≈A; Frankie 2026-06-16 定)
        pb = a.get("netsales", 0) - abs(a.get("pf", 0))
        f["回款RMB"] = round(pb, 2)
        f["回款率"] = round(pb / a["sales"], 4) if a["sales"] else None
    if ptype == "ml" and a.get("sales"):     # 美客多回款: ml-sync 未抓 Mercado Pago settlement → fallback=营收−ML佣金−退款(Frankie 2026-06-16 定)
        f["回款RMB"] = round(a["payback"], 2)
        f["回款率"] = round(a["payback"] / a["sales"], 4) if a["sales"] else None
    return {k: v for k, v in f.items() if v not in (None, "")}


def _agg_find(T, ym_dash, cat, plat, shop):
    for r in _bitable_all(T, IDX_APP, TOTAL_TBL):
        ff = r["fields"]
        if ft(ff.get("月份")) == ym_dash and ft(ff.get("渠道大类")) == cat and ft(ff.get("平台")) == (plat or "") and ft(ff.get("店铺")) == shop:
            return r["record_id"]
    return None


def _agg_upsert(T, ym_dash, cat, plat, shop, fields):
    H = {"Authorization": f"Bearer {T}", "Content-Type": "application/json"}
    target = _agg_find(T, ym_dash, cat, plat, shop)
    if target:
        requests.put(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{TOTAL_TBL}/records/{target}", headers=H, json={"fields": fields}, timeout=20)
        return "update"
    requests.post(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{TOTAL_TBL}/records", headers=H, json={"fields": fields}, timeout=20)
    return "create"


def _agg_delete(T, ym_dash, cat, plat, shop):
    """gate-fail 时若总表已有该行→删除(保证总表只含审过的渠道)。返回是否删了。"""
    rid = _agg_find(T, ym_dash, cat, plat, shop)
    if rid:
        requests.delete(f"{FEISHU}/bitable/v1/apps/{IDX_APP}/tables/{TOTAL_TBL}/records/{rid}",
                        headers={"Authorization": f"Bearer {T}"}, timeout=20)
        return True
    return False


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
            rm = _agg_delete(T, ym_dash, cat, plat, shop)
            skipped.append({"shop": shop, "why": "数据缺漏:报表空/0销售", "gate": "fail", "removed_stale": rm}); continue
        # 审计 gate: 成本缺失>阈值 → 挂起不灌(逐渠道不阻塞; 修好下次cron自动灌); 已有stale行则删
        cm_amt = a.get("cm_amt", 0); pct = (cm_amt / a["sales"] * 100) if a["sales"] else 0
        if cm_amt > 0 and pct > GATE_PCT:
            rm = _agg_delete(T, ym_dash, cat, plat, shop)
            skipped.append({"shop": shop, "why": f"审计未过:成本缺失{a['cm_n']}SKU ¥{round(cm_amt)}({pct:.1f}%>{GATE_PCT:.0f}%)", "gate": "fail", "removed_stale": rm}); continue
        fields = _agg_fields(ym_dash, cat, plat, shop, brand, a, url, ptype)
        act = _agg_upsert(T, ym_dash, cat, plat, shop, fields)
        done.append({"shop": shop, "act": act, "sales": round(a["sales"]), "margin": round(a["margin"]), "payback": round(a["payback"]),
                     "cost_missing": (f"{a['cm_n']}SKU ¥{round(cm_amt)}" if a.get("cm_n") else None)})
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


# ===================== 月度汇报推送 (/report-monthly) =====================
# 全景卡 → 财务部+Frankie; 单渠道卡 → 各渠道负责人(只收自己渠道)。frankie_only=首跑只发Frankie预览。
# 平台值 → 渠道负责人(职务实时查 jt / 固定 open_id)。平台空(国内电商)按 key="国内电商"。
REPORT_OWNERS = {
    "亚马逊": ("jt", DEPT_XB, "亚马逊运营"),
    "美客多": ("jt", DEPT_XB, "美客多运营"),
    "独立站": ("jt", DEPT_ZW, "独立站运营"),
    "国内电商": ("jt", DEPT_GN, "国内平台运营"),
    "沃尔玛": ("fixed", [("ou_35aa6883c0598bac5c7e06fcb06f7c4d", "林明坚")]),
    "速卖通": ("fixed", [("ou_274ee5199a763b7ec97980cd54e3fecb", "赵伟俊")]),
    "线下": ("fixed", [("ou_1314a50710f13f76d1c507fbc9260403", "马建威")]),
    "B2B": ("fixed", [("ou_8491e70bf70b490f8610852d0550c2dc", "冼浩华")]),
}


def _resolve_owners(T, plat):
    m = REPORT_OWNERS.get(plat or "国内电商")
    if not m: return []
    if m[0] == "fixed": return m[1]
    _, did, jt = m
    names = _dept_members(T, did)
    return [(oid, names.get(oid, "")) for oid, j in _dept_users_jt(T, did) if jt in j]


def _send_card(T, oid, card):
    try:
        return requests.post(f"{FEISHU}/im/v1/messages?receive_id_type=open_id",
                             headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                             json={"receive_id": oid, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}, timeout=20).json().get("code")
    except Exception:
        return -1


def _fmt(x): return f"{x:,.0f}"


def _monthly_report(frankie_only=False, dry_run=False):
    T = tok(); _dept_jt_cache.clear()
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    ym_dash = last.strftime("%Y-%m")
    rows = [r for r in _bitable_all(T, IDX_APP, TOTAL_TBL) if ft(r["fields"].get("月份")) == ym_dash]
    if not rows:
        return {"month": ym_dash, "note": "总表本月无数据"}
    rows.sort(key=lambda r: -_aggnum(r["fields"].get("全额毛利RMB")))
    present = set(ft(r["fields"].get("店铺")) for r in rows)
    pending = [shop for (_, _, _, shop, _, _) in AGG_REPORTS if shop not in present]
    tot_s = tot_m = 0
    rowmeta = []
    for r in rows:
        f = r["fields"]; s = _aggnum(f.get("销售额RMB")); m = _aggnum(f.get("全额毛利RMB")); tot_s += s; tot_m += m
        plat = ft(f.get("平台")); cat = ft(f.get("渠道大类")); pb = f.get("回款RMB")
        owners = _resolve_owners(T, plat)
        rowmeta.append({"plat": plat or cat, "cat": cat, "shop": ft(f.get("店铺")), "s": s, "m": m,
                        "mr": (m / s if s else 0), "pb": _aggnum(pb) if pb else None, "owners": owners})
    # 全景卡 (财务部+Frankie)
    tbl = "**渠道 | 销售 | 毛利 | 毛利率 | 回款 | 负责人**\n"
    for x in rowmeta:
        ons = "/".join(n for _, n in x["owners"]) or "—"
        pbs = _fmt(x["pb"]) if x["pb"] is not None else "—"
        tbl += f"{x['plat']} | {_fmt(x['s'])} | {_fmt(x['m'])} | {x['mr']*100:.1f}% | {pbs} | {ons}\n"
    tbl += f"**合计 | {_fmt(tot_s)} | {_fmt(tot_m)} | {tot_m/tot_s*100:.1f}% | | **"
    pend_txt = ("\n\n⏳ **待核未灌**：" + "、".join(pending) + "（审计未过/报表0，见审计卡，修复后次日自动灌）") if pending else ""
    overview = {"config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🟡 [FIN·P2] 全渠道毛利月度汇报 · {ym_dash}"}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": tbl + pend_txt}},
                             {"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": "📊 已审过才灌总表。回款列：跨境=领星实数，美客多/国内电商待财务口径。"}}]}
    sent = {"overview": [], "channel": []}
    if not dry_run:
        targets = {FRANKIE} if frankie_only else ({FRANKIE} | set(_dept_members(T, FIN_DEPT).keys()))
        for oid in targets:
            sent["overview"].append(_send_card(T, oid, overview))
    # 单渠道卡 → 各负责人
    for x in rowmeta:
        if not x["owners"]: continue
        cm = ""  # (灌入行无成本缺失阻断, 略)
        card = {"config": {"wide_screen_mode": True},
                "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": f"🟡 [FIN·P2] {x['plat']}毛利月度汇报 · {ym_dash}"}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content":
                    f"**{x['plat']}**（{x['shop']}）\n销售 **¥{_fmt(x['s'])}** · 毛利 **¥{_fmt(x['m'])}**（{x['mr']*100:.1f}%）"
                    + (f" · 回款 ¥{_fmt(x['pb'])}" if x['pb'] is not None else "") + cm}}]}
        for oid, nm in x["owners"]:
            tgt = FRANKIE if frankie_only else oid
            if not dry_run:
                sent["channel"].append({"plat": x["plat"], "to": nm if not frankie_only else f"Frankie(代{nm})", "code": _send_card(T, tgt, card)})
            else:
                sent["channel"].append({"plat": x["plat"], "to": nm, "code": "dry"})
    return {"month": ym_dash, "rows": len(rows), "total_sales": round(tot_s), "total_margin": round(tot_m),
            "pending": pending, "frankie_only": frankie_only, "dry_run": dry_run, "sent": sent}


def _company_md(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def _company_fields(items):
    fields = []
    for label, value in items:
        if value is None or value == "":
            value = "-"
        fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}})
    return {"tag": "div", "fields": fields}


def _company_note(text):
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _company_button(text, payload, button_type="default"):
    return {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": button_type, "value": payload}


def _company_base_card(title, template, elements):
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _company_payload(action, run_id, card_type, card_id, *, gap_id="", platform="", period="", decision="", nonce=""):
    nonce = nonce or str(_now_ms())
    target_id = gap_id or run_id
    return {"action": action, "schema_version": COMPANY_CARD_SCHEMA, "run_id": run_id,
            "card_type": card_type, "card_id": card_id, "gap_id": gap_id, "platform": platform,
            "period": period, "decision": decision,
            "idempotency_key": _company_idem(action, run_id, card_id, target_id, nonce)}


GAP_TYPE_LABELS = {
    "api_error": "自动取数异常",
    "source_file_gap": "平台报表资料缺口",
    "master_data_gap": "采购/成本/物流资料缺口",
    "finance_rule_gap": "财务口径待确认",
    "zero_report_confirm": "零销售确认",
    "workflow_gap": "流程接入缺口",
}

DATA_MODE_LABELS = {
    "api": "自动取数",
    "manual": "人工资料",
    "hybrid": "自动同步 + 成本物流维护",
    "ledger": "业务台账",
}


def _company_label(mapping, value):
    raw = ft(value)
    return mapping.get(raw, raw or "-")


def _company_period_label(period):
    p = ft(period)
    if "smoke" in p:
        return "测试账期"
    return p or "-"


def _company_processed_card(title, message, ok=True, details=None):
    details = details or {}
    elements = [_company_md(message)]
    visible = [(k, v) for k, v in details.items() if v]
    if visible:
        elements += [{"tag": "hr"}, _company_fields(visible)]
    elements.append(_company_note("此卡片已处理，无需重复点击。"))
    return _company_base_card(title, "green" if ok else "grey", elements)


def _company_gap_card(run, gap, *, test_mode=False):
    rf = run.get("fields", {})
    gf = gap.get("fields", {})
    run_id = ft(rf.get("run_id"))
    gap_id = ft(gf.get("gap_id"))
    period = ft(rf.get("期间")) or ft(gf.get("期间"))
    platform = ft(rf.get("平台")) or ft(gf.get("平台"))
    gap_type = ft(gf.get("缺口责任类型")) or ft(rf.get("缺口责任类型"))
    detail = ft(gf.get("缺口说明")) or ft(gf.get("证据"))
    blocker = ft(rf.get("当前阻断方")) or ft(gf.get("责任人")) or "-"
    card_id = _company_card_id("p0_gap", run_id, gap_id)
    nonce = str(_now_ms())
    note = "测试卡，仅发给 Frankie；不会通知运营或财务。" if test_mode else "请确认后再点击，处理结果会自动更新在这张卡片上。"
    return _company_base_card("🔴 [FIN·P0] 毛利缺口待处理", "red", [
        _company_fields([
            ("平台", platform),
            ("期间", _company_period_label(period)),
            ("阻断方", blocker),
            ("缺口类型", _company_label(GAP_TYPE_LABELS, gap_type)),
        ]),
        {"tag": "hr"},
        _company_md(
            f"**缺口说明**\n{detail or '待补充'}\n\n"
            "P0 关闭前不能进入财务终审，也不能灌总表。"
        ),
        {"tag": "hr"},
        {"tag": "action", "actions": [
            _company_button("成本已补齐",
                            _company_payload("company_profit_gap_resolved", run_id, "p0_gap", card_id,
                                             gap_id=gap_id, platform=platform, period=period, nonce=nonce),
                            "primary"),
            _company_button("本月例外终审",
                            _company_payload("company_profit_gap_exception", run_id, "p0_gap", card_id,
                                             gap_id=gap_id, platform=platform, period=period,
                                             decision="exception", nonce=nonce)),
        ]},
        _company_note(note),
    ])


def _company_finance_card(run, *, test_mode=False):
    rf = run.get("fields", {})
    run_id = ft(rf.get("run_id"))
    period = ft(rf.get("期间"))
    platform = ft(rf.get("平台"))
    card_id = _company_card_id("finance_confirm", run_id, platform)
    nonce = str(_now_ms())
    note = "测试卡，仅发给 Frankie；不会通知财务。" if test_mode else "请确认后再点击，处理结果会自动更新在这张卡片上。"
    return _company_base_card("🟡 [FIN·P2] 毛利报表终审", "orange", [
        _company_fields([
            ("平台", platform),
            ("期间", _company_period_label(period)),
            ("数据方式", _company_label(DATA_MODE_LABELS, rf.get("data_mode"))),
            ("数据状态", ft(rf.get("数据状态"))),
            ("报表状态", ft(rf.get("报表状态"))),
            ("总表状态", ft(rf.get("总表状态")) or "未灌总表"),
        ]),
        {"tag": "hr"},
        _company_md(
            "请确认本月毛利报表是否可以终审通过。\n\n"
            "提交时系统会再次检查 P0；仍有未处理缺口时会自动拒绝通过。"
        ),
        {"tag": "hr"},
        {"tag": "action", "actions": [
            _company_button("终审通过",
                            _company_payload("company_profit_finance_approve", run_id, "finance_confirm", card_id,
                                             platform=platform, period=period, decision="approve", nonce=nonce),
                            "primary"),
            _company_button("退回补缺口",
                            _company_payload("company_profit_finance_return", run_id, "finance_confirm", card_id,
                                             platform=platform, period=period, decision="return_p0", nonce=nonce)),
        ]},
        _company_note(note),
    ])


def _send_event_card_union(T, union_id, card):
    return requests.post(f"{FEISHU}/im/v1/messages?receive_id_type=union_id",
                         headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                         json={"receive_id": union_id, "msg_type": "interactive",
                               "content": json.dumps(card, ensure_ascii=False)}, timeout=20).json()


def _send_event_card_open_id(T, open_id, card):
    return requests.post(f"{FEISHU}/im/v1/messages?receive_id_type=open_id",
                         headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                         json={"receive_id": open_id, "msg_type": "interactive",
                               "content": json.dumps(card, ensure_ascii=False)}, timeout=20).json()


def _patch_message_card(T, message_id, card):
    if not message_id:
        return {"code": -1, "msg": "no message_id"}
    return requests.patch(f"{FEISHU}/im/v1/messages/{message_id}",
                          headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                          json={"content": json.dumps(card, ensure_ascii=False)}, timeout=20).json()


def _append_company_message_id(T, run_id, message_id):
    if not message_id:
        return
    rec = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)
    if not rec:
        return
    old = ft(rec.get("fields", {}).get("原卡message_ids"))
    parts = [p for p in old.split(",") if p] if old else []
    if message_id not in parts:
        parts.append(message_id)
    _company_update_run(T, run_id, {"原卡message_ids": ",".join(parts), "最后动作": "append_message_id"})


def _deep_get(obj, path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _parse_card_value(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _company_callback_context(body):
    value = (_deep_get(body, ["event", "action", "value"]) or
             _deep_get(body, ["body", "event", "action", "value"]) or
             _deep_get(body, ["action", "value"]) or body.get("card_action") or body.get("value") or {})
    operator_open_id = (_deep_get(body, ["event", "operator", "open_id"]) or
                        _deep_get(body, ["body", "event", "operator", "open_id"]) or
                        _deep_get(body, ["operator", "open_id"]) or body.get("operator_open_id") or "")
    message_id = (_deep_get(body, ["event", "context", "open_message_id"]) or
                  _deep_get(body, ["body", "event", "context", "open_message_id"]) or
                  _deep_get(body, ["event", "open_message_id"]) or
                  _deep_get(body, ["data", "card_open_message_id"]) or
                  body.get("open_message_id") or body.get("message_id") or "")
    chat_id = (_deep_get(body, ["event", "context", "open_chat_id"]) or
               _deep_get(body, ["body", "event", "context", "open_chat_id"]) or
               body.get("open_chat_id") or body.get("chat_id") or "")
    form_value = (_deep_get(body, ["event", "action", "form_value"]) or
                  _deep_get(body, ["body", "event", "action", "form_value"]) or
                  body.get("card_form_value") or {})
    return {"value": _parse_card_value(value), "operator_open_id": operator_open_id,
            "message_id": message_id, "chat_id": chat_id, "form_value": form_value}


def _company_open_p0_count(T, run_id):
    n = 0
    for rec in _bitable_all(T, IDX_APP, COMPANY_GAP_TBL):
        f = rec.get("fields", {})
        if ft(f.get("run_id")) != run_id:
            continue
        if ft(f.get("P级")) == "P0" and ft(f.get("处理结果")) not in ("已补件", "确认例外", "已关闭"):
            n += 1
    return n


def _patch_or_fallback(ctx, card):
    T = event_tok()
    if ctx.get("message_id"):
        patched = _patch_message_card(T, ctx["message_id"], card)
        if patched.get("code") == 0:
            return {"patched_original_card": True, "response": patched}
    if ctx.get("operator_open_id"):
        sent = _send_event_card_open_id(T, ctx["operator_open_id"], card)
        if sent.get("code") == 99992361 and FRANKIE_UNION_ID:
            union_sent = _send_event_card_union(T, FRANKIE_UNION_ID, card)
            return {"fallback_frankie_union_card": True, "open_id_response": sent, "response": union_sent}
        return {"fallback_operator_card": True, "response": sent}
    return {"patched_original_card": False}


def _handle_company_callback(body):
    T = tok()
    ctx = _company_callback_context(body)
    value = ctx["value"]
    action = str(value.get("action") or "")
    if not action.startswith("company_profit_"):
        return {"ignored": True, "reason": "not_company_profit_action", "action": action}

    run_id = str(value.get("run_id") or "")
    gap_id = str(value.get("gap_id") or "")
    idempotency_key = str(value.get("idempotency_key") or _payload_hash(value))
    if _company_audit_exists(T, idempotency_key):
        current_run = (_bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {}).get("fields", {})
        current_gap = (_bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id) or {}).get("fields", {}) if gap_id else {}
        card = _company_processed_card("✅ 毛利卡片已处理", "这次点击已经记录过，重复点击不会再次改变状态。",
                                       details={"平台": ft(current_run.get("平台")),
                                                "期间": _company_period_label(current_run.get("期间")),
                                                "当前状态": ft(current_run.get("报表状态")),
                                                "缺口状态": ft(current_gap.get("处理结果"))})
        return {"duplicate": True, "patch": _patch_or_fallback(ctx, card)}

    before = {"run": (_bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {}).get("fields", {}),
              "gap": (_bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id) or {}).get("fields", {}) if gap_id else {}}
    ok = True
    target_type = "run"
    target_id = run_id
    result_message = "已处理。"

    if action == "company_profit_gap_resolved":
        target_type, target_id = "gap", gap_id
        _company_update_gap(T, gap_id, {"处理结果": "已补件", "是否可进财务终审": "true",
                                        "来源message_id": ctx["message_id"], "最后动作": action})
        _company_update_run(T, run_id, {"报表状态": "待AI重跑", "当前阻断方": "AI自动化",
                                        "P0数量": str(_company_open_p0_count(T, run_id)), "最后动作": action})
        result_message = "已记录补件，报表将进入 AI 重跑。"
    elif action == "company_profit_gap_exception":
        target_type, target_id = "gap", gap_id
        _company_update_gap(T, gap_id, {"处理结果": "确认例外", "是否可进财务终审": "true",
                                        "来源message_id": ctx["message_id"], "最后动作": action})
        open_p0 = _company_open_p0_count(T, run_id)
        _company_update_run(T, run_id, {"报表状态": "待财务终审" if open_p0 == 0 else "P0待处理",
                                        "当前阻断方": "财务部" if open_p0 == 0 else "负责人/采购/财务",
                                        "P0数量": str(open_p0), "最后动作": action})
        result_message = "已记录本月例外，系统会按剩余缺口判断是否进入财务终审。"
    elif action == "company_profit_finance_approve":
        open_p0 = _company_open_p0_count(T, run_id)
        if open_p0 > 0:
            ok = False
            result_message = f"仍有 {open_p0} 个 P0 缺口未处理，本次不能终审通过。"
        else:
            _company_update_run(T, run_id, {"报表状态": "财务通过", "当前阻断方": "AI自动化",
                                            "总表状态": "待灌总表", "P0数量": "0",
                                            "最后动作": action})
            result_message = "财务终审已通过，下一步进入总表灌表队列。"
    elif action == "company_profit_finance_return":
        _company_update_run(T, run_id, {"报表状态": "P0待处理", "当前阻断方": "财务部",
                                        "P0数量": str(max(1, _company_open_p0_count(T, run_id))),
                                        "最后动作": action})
        result_message = "已退回补缺口，暂不能进入总表灌表。"
    else:
        ok = False
        result_message = "这张卡片的按钮动作无法识别，请联系 AI 自动化处理。"

    after = {"run": (_bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {}).get("fields", {}),
             "gap": (_bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id) or {}).get("fields", {}) if gap_id else {}}
    _company_write_audit(T, idempotency_key, action, ctx["operator_open_id"], run_id, target_type, target_id,
                         before, after, {"value": value, "form_value": ctx["form_value"]},
                         "ok" if ok else "blocked", ctx["message_id"])
    after_run = after.get("run") or {}
    after_gap = after.get("gap") or {}
    result_details = {
        "平台": ft(after_run.get("平台")),
        "期间": _company_period_label(after_run.get("期间")),
        "当前状态": ft(after_run.get("报表状态")),
        "缺口状态": ft(after_gap.get("处理结果")) if after_gap else "",
        "剩余P0": ft(after_run.get("P0数量")),
    }
    card = _company_processed_card("✅ 毛利卡片已处理" if ok else "⚠️ 毛利卡片未通过",
                                   result_message, ok=ok, details=result_details)
    return {"ok": ok, "action": action, "run_id": run_id, "patch": _patch_or_fallback(ctx, card)}


def _last_month():
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    return last.strftime("%Y-%m")


@app.post("/report-monthly")
async def report_monthly(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    return _monthly_report(frankie_only=q.get("frankie_only") == "true", dry_run=q.get("dry_run") == "true")


@app.post("/profit-workflow/seed")
async def profit_workflow_seed(request: Request):
    """Seed company-level gross-profit workflow runs into Finance Base ledger."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    period = q.get("period") or _last_month()
    platform = q.get("platform")
    T = tok()
    platform_ids = [platform] if platform else list(COMPANY_PLATFORM_REGISTRY.keys())
    rows = []
    for pid in platform_ids:
        rec = _company_seed_run(T, period, pid)
        rows.append({"platform": pid, "run_id": ft(rec.get("fields", {}).get("run_id")),
                     "record_id": rec.get("record_id")})
    return {"period": period, "seeded": rows, "run_table": COMPANY_RUN_TBL}


@app.post("/profit-workflow/test-cards")
async def profit_workflow_test_cards(request: Request):
    """P0 smoke: create Base ledger rows and optionally send sample cards to Frankie only."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    period = q.get("period") or _last_month()
    platform_id = q.get("platform") or "funlabswitch"
    card_type = q.get("card_type") or "both"  # p0_gap / finance / both
    send = q.get("send") == "true"
    T = tok()
    run = _company_seed_run(T, period, platform_id)
    rf = run.get("fields", {})
    run_id = ft(rf.get("run_id"))
    platform = ft(rf.get("平台"))
    gap = None
    cards = {}
    if card_type in ("p0_gap", "both"):
        detail = "采购成本缺口阻断，不绕过 5% gate；负责人补成本或 Frankie/财务确认例外后才能进终审。"
        gap = _company_create_gap(T, run_id, period, platform, "master_data_gap", detail, owner=ft(rf.get("当前阻断方")))
        _company_update_run(T, run_id, {"报表状态": "P0待处理", "当前阻断方": "采购/负责人",
                                        "缺口责任类型": "master_data_gap", "P0数量": "1",
                                        "最后动作": "send_p0_test_card"})
        run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
        cards["p0_gap"] = _company_gap_card(run, gap, test_mode=True)
    if card_type in ("finance", "both"):
        if card_type == "finance":
            _company_update_run(T, run_id, {"报表状态": "待财务终审", "当前阻断方": "财务部",
                                            "P0数量": str(_company_open_p0_count(T, run_id)),
                                            "最后动作": "send_finance_test_card"})
            run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
        cards["finance"] = _company_finance_card(run, test_mode=True)

    sent = {}
    if send:
        ET = event_tok()
        for name, card in cards.items():
            res = _send_event_card_union(ET, FRANKIE_UNION_ID, card)
            mid = (res.get("data") or {}).get("message_id")
            if mid:
                _append_company_message_id(T, run_id, mid)
            sent[name] = {"code": res.get("code"), "message_id": mid}
    return {"period": period, "platform": platform_id, "run_id": run_id, "frankie_only": True,
            "send": send, "sent": sent, "cards": cards if not send else list(cards.keys()),
            "ledger": {"run_table": COMPANY_RUN_TBL, "gap_table": COMPANY_GAP_TBL, "audit_table": COMPANY_AUDIT_TBL}}


@app.post("/profit-workflow/callback")
async def profit_workflow_callback(request: Request):
    """n8n Event Hub forwards company_profit_* card.action.trigger payloads here."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    body = await request.json()
    return _handle_company_callback(body)


@app.get("/health")
def health(): return {"ok": True}


@app.post("/audit")
async def audit(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    return do_audit()
