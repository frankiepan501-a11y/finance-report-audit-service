# -*- coding: utf-8 -*-
"""全渠道毛利报表月度审计 (Zeabur)。每月9号16:00 n8n cron → POST /audit。
检查: 数据缺漏(空报表) / 采购成本覆盖(有销售但cg=0) / 物流头程覆盖。
异常 → 飞书卡片发财务部 + Frankie, 列 渠道/店铺/负责人/异常, 让财务跟运营核实。
口径: 只 flag「销售额>0 且 成本=0」(真异常); 销售=0的0成本行忽略。"""
import os, json, datetime, hashlib, time, re, threading
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
WXD_UNION_ID = os.environ.get("WXD_UNION_ID", "on_854142cacab1e17fe75cb5622ed5112d")
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
DOMESTIC_ECOM_TASK_APP = os.environ.get("DOMESTIC_ECOM_TASK_APP_TOKEN", "IKyGb1jydaZW7msBzAicViiWngg")
DOMESTIC_ECOM_TASK_TBL = os.environ.get("DOMESTIC_ECOM_TASK_TABLE_ID", "tblMYHXRHZ0GaqMh")
COMPANY_CARD_SCHEMA = "company_profit_card_v3"
COMPANY_CARD_TEMPLATE_VERSION = "v5-owner-gap-dispatch"
FUNLABSWITCH_SHOPIFY_CUTOFF = "2026-07"
COMPANY_CALLBACK_SEND_NEXT = os.environ.get("COMPANY_CALLBACK_SEND_NEXT", "false").lower() == "true"
COMPANY_GENERATOR_ENABLED = os.environ.get("COMPANY_GENERATOR_ENABLED", "false").lower() == "true"
COMPANY_GENERATOR_TIMEOUT = int(os.environ.get("COMPANY_GENERATOR_TIMEOUT", "30"))
COMPANY_GENERATOR_REQUEST_TIMEOUT = int(os.environ.get(
    "COMPANY_GENERATOR_REQUEST_TIMEOUT", str(min(COMPANY_GENERATOR_TIMEOUT, 25))
))
COMPANY_GENERATOR_POLL_TIMEOUT = int(os.environ.get("COMPANY_GENERATOR_POLL_TIMEOUT", "360"))
COMPANY_GENERATOR_POLL_INTERVAL = float(os.environ.get("COMPANY_GENERATOR_POLL_INTERVAL", "5"))
COMPANY_GENERATOR_ALLOWED_PLATFORMS = {
    p.strip() for p in os.environ.get("COMPANY_GENERATOR_ALLOWED_PLATFORMS", "").split(",") if p.strip()
}
_N8N_WEBHOOK_BASE = (os.environ.get("N8N_WEBHOOK_BASE_URL")
                     or os.environ.get("N8N_PUBLIC_BASE_URL")
                     or os.environ.get("N8N_BASE_URL")
                     or "https://frankiepan501.zeabur.app").rstrip("/")
if _N8N_WEBHOOK_BASE.endswith("/api/v1"):
    _N8N_WEBHOOK_BASE = _N8N_WEBHOOK_BASE[:-7].rstrip("/")
N8N_WEBHOOK_BASE_URL = _N8N_WEBHOOK_BASE
_N8N_API_BASE = (os.environ.get("N8N_API_BASE_URL") or os.environ.get("N8N_BASE_URL") or N8N_WEBHOOK_BASE_URL).rstrip("/")
if not _N8N_API_BASE.endswith("/api/v1"):
    _N8N_API_BASE = f"{_N8N_API_BASE}/api/v1"
N8N_API_BASE_URL = _N8N_API_BASE
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
_COMPANY_ASYNC_JOBS = {}
_COMPANY_ASYNC_LOCK = threading.Lock()

COMPANY_PLATFORM_REGISTRY = {
    "amazon": {"name": "Amazon", "platform": "亚马逊", "site": "亚马逊全站", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部", "maturity": "confirmed", "generator_type": "n8n_webhook", "workflow_id": "CyapOmKK0hyIJoXY", "generator_method": "GET", "generator_path": "trigger-amazon-profit"},
    "walmart": {"name": "Walmart", "platform": "沃尔玛", "site": "沃尔玛全站", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部", "maturity": "confirmed", "generator_type": "n8n_webhook", "workflow_id": "HETbzME852KlYpFl", "generator_method": "GET", "generator_path": "trigger-walmart-profit"},
    "mercadolibre": {"name": "Mercado Libre", "platform": "美客多", "site": "美客多店铺组", "data_mode": "hybrid", "data_status": "数据已就绪", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部", "maturity": "confirmed", "generator_type": "ml_sync_service", "service_base_url": "https://ml-sync.zeabur.app", "generator_note": "按 seller_id 同步，不是公司级一键全店月报触发。", "generator_requires": "seller_id"},
    "funlab_net": {"name": "funlab.net", "platform": "独立站", "site": "funlab.net", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部", "maturity": "confirmed", "generator_type": "n8n_webhook", "workflow_id": "2q7WSFS5G9zQpfcN", "generator_method": "GET", "generator_path": "trigger-funlabnet-profit"},
    "powkong": {"name": "Powkong", "platform": "独立站", "site": "powkong.com", "data_mode": "api", "data_status": "取数完成", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部", "maturity": "confirmed", "generator_type": "n8n_webhook", "workflow_id": "rkG32295bx3dVcRh", "generator_method": "GET", "generator_path": "trigger-powkong-shopify-admin-profit"},
    "domestic_ecom": {"name": "国内电商", "platform": "国内电商", "site": "国内电商店铺组", "data_mode": "manual", "data_status": "资料已提交", "report_status": "待财务终审", "blocker_type": "", "blocker": "财务部", "maturity": "confirmed", "generator_type": "service_endpoint", "service_base_url": "https://domestic-ecom-profit.zeabur.app", "service_endpoint": "/profit/run", "generator_method": "POST", "auth_token_env": "DOMESTIC_ECOM_PROFIT_TOKEN", "generator_requires": "source_record_id", "generator_lookup": "domestic_summary_record", "generator_note": "按月份自动查国内电商任务台的月度报表汇总 record_id 后触发。"},
    "funlabswitch": {"name": "funlabswitch.com", "platform": "独立站", "site": "funlabswitch.com", "data_mode": "hybrid", "data_status": "待成本维护", "report_status": "P0待处理", "blocker_type": "master_data_gap", "blocker": "采购/负责人", "maturity": "blocked", "generator_type": "n8n_webhook", "workflow_id": "s9u91925K049t7ud", "generator_method": "GET", "generator_path": "trigger-funlabswitch-profit", "generator_note": "2026-06 保留历史 Shopline/成本缺口收口；2026-07 起迁移 Shopify API，与 funlab.net/Powkong 统一。"},
    "aliexpress": {"name": "AliExpress", "platform": "速卖通", "site": "速卖通店铺组", "data_mode": "api", "data_status": "取数完成", "report_status": "待接统一终审", "blocker_type": "workflow_gap", "blocker": "AI自动化", "maturity": "partial", "generator_type": "n8n_webhook", "workflow_id": "eQBUjKcBr30zgBgy", "generator_method": "GET", "generator_path": "trigger-aliexpress-profit"},
    "tiktok_shop": {"name": "TikTok Shop", "platform": "TikTok Shop", "site": "TikTok Shop店铺组", "data_mode": "api", "data_status": "取数完成", "report_status": "待接统一终审", "blocker_type": "workflow_gap", "blocker": "AI自动化", "maturity": "partial", "generator_type": "n8n_webhook", "workflow_id": "Zw17LKlAL6W9TC0V", "generator_method": "GET", "generator_path": "trigger-tiktok-profit"},
    "b2b": {"name": "B2B", "platform": "B2B", "site": "B2B业务台账", "data_mode": "ledger", "data_status": "台账已就绪", "report_status": "待接台账模式", "blocker_type": "workflow_gap", "blocker": "AI自动化", "maturity": "partial", "generator_type": "ledger_service", "generator_note": "待补 B2B 台账完整性审计与总表触发。"},
    "offline": {"name": "国内线下", "platform": "国内线下", "site": "国内线下业务台账", "data_mode": "ledger", "data_status": "台账已就绪", "report_status": "待接台账模式", "blocker_type": "workflow_gap", "blocker": "AI自动化", "maturity": "partial", "generator_type": "ledger_service", "generator_note": "待补国内线下台账完整性审计与总表触发。"},
    "temu": {"name": "TEMU", "platform": "TEMU", "site": "TEMU店铺组", "data_mode": "manual", "data_status": "待资料提交", "report_status": "待定口径", "blocker_type": "finance_rule_gap", "blocker": "财务/负责人", "maturity": "unconfirmed", "generator_type": "", "generator_note": "待完成平台口径与 A/B 逐字段核实。"},
    "taobao": {"name": "淘宝", "platform": "淘宝", "site": "淘宝店铺组", "data_mode": "manual", "data_status": "待资料提交", "report_status": "待定口径", "blocker_type": "finance_rule_gap", "blocker": "财务/负责人", "maturity": "unconfirmed", "generator_type": "", "generator_note": "待完成平台口径与 A/B 逐字段核实。"},
    "pinduoduo": {"name": "拼多多", "platform": "拼多多", "site": "拼多多店铺组", "data_mode": "manual", "data_status": "待资料提交", "report_status": "待定口径", "blocker_type": "finance_rule_gap", "blocker": "财务/负责人", "maturity": "unconfirmed", "generator_type": "", "generator_note": "待完成平台口径与 A/B 逐字段核实。"},
}

COMPANY_REPORT_FIELD_BY_PLATFORM = {
    "amazon": "亚马逊毛利报表",
    "walmart": "沃尔玛毛利报表",
    "mercadolibre": "美客多毛利报表",
    "funlab_net": "独立站funlab.net毛利报表",
    "powkong": "独立站Powkong Admin API毛利报表",
    "domestic_ecom": "国内电商毛利报表",
    "funlabswitch": "独立站funlabswitch毛利报表",
    "aliexpress": "速卖通毛利报表",
    "tiktok_shop": "TikTok Shop毛利报表",
    "temu": "TEMU毛利报表",
}
COMPANY_AGG_PLATFORM_BY_REPORT_FIELD = {v: k for k, v in COMPANY_REPORT_FIELD_BY_PLATFORM.items()}
COMPANY_AGG_PLATFORM_BY_REPORT_FIELD["独立站毛利报表"] = "funlab_net"
COMPANY_AGG_APPROVED_STATUSES = {"财务通过", "已灌总表", "已归档"}

COMPANY_V1_PLATFORM_IDS = ["domestic_ecom", "mercadolibre", "amazon", "walmart", "funlab_net", "powkong"]
COMPANY_P0_PLATFORM_IDS = ["funlabswitch"]
COMPANY_P1_PLATFORM_IDS = ["aliexpress", "tiktok_shop", "b2b", "offline"]
COMPANY_P2_PLATFORM_IDS = ["temu", "taobao", "pinduoduo"]


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
FINANCE_REPORT_OWNER = os.environ.get("FINANCE_REPORT_OWNER_OPEN_ID", FIN["莫莉莉"])
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


def _contact_user(T, open_id):
    if not open_id:
        return {}
    try:
        r = requests.get(f"{FEISHU}/contact/v3/users/{open_id}?user_id_type=open_id",
                         headers={"Authorization": f"Bearer {T}"}, timeout=20).json()
        return (r.get("data") or {}).get("user") or {}
    except Exception:
        return {}


def _union_id_for_open_id(T, open_id):
    if open_id == FRANKIE:
        return FRANKIE_UNION_ID
    if open_id == WXD:
        return WXD_UNION_ID
    return ft(_contact_user(T, open_id).get("union_id"))


def _company_find_open_id_by_name(T, name):
    name = ft(name).strip()
    if not name:
        return ""
    for did in (DEPT_XB, DEPT_ZW, DEPT_GN, FIN_DEPT):
        try:
            members = _dept_members(T, did)
            for oid, nm in members.items():
                if ft(nm).strip() == name:
                    return oid
        except Exception:
            continue
    if name in COMPANY_OPERATOR_OPEN_ID_BY_NAME:
        return COMPANY_OPERATOR_OPEN_ID_BY_NAME[name]
    return ""


def _company_recipient_by_name(T, name):
    clean = ft(name).strip()
    oid = _company_find_open_id_by_name(T, clean)
    if not oid:
        return None
    union_id = _union_id_for_open_id(T, oid)
    if not union_id:
        return None
    return {"name": clean, "open_id": oid, "union_id": union_id}


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

# 运营缺口卡需要按报表里的 Listing负责人 精准发人。优先用通讯录实时取 union_id；
# 这里保留常用姓名的 open_id 兜底，避免卡片分派因搜索接口波动中断。
COMPANY_OPERATOR_OPEN_ID_BY_NAME = {
    "黄奕纯": "ou_3a80e361d1e8a1d23ead015b6a2a8369",
    "陈翔宇": "ou_ed76cf4c789f13fda0921c3e8f6acf40",
    "余培霓": "ou_59aa463ab360202b213480e9bae5ced1",
    "林明坚": "ou_e1b96884de4085554369fe1d1c5a0aea",
}


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


def _transfer_owner(T, token, typ, oid):
    if not token or not typ or not oid:
        return None
    try:
        r = requests.post(f"{FEISHU}/drive/v1/permissions/{token}/members/transfer_owner?type={typ}",
                          headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                          json={"member_type": "openid", "member_id": oid}, timeout=20).json()
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
    finance_full_access = {FRANKIE: "Frankie", WXD: "吴晓丹", FINANCE_REPORT_OWNER: "财务负责人"}
    finance_full_access.update(fin)
    granted = []
    for fld, url in links.items():
        token, typ = _parse_link(url)
        if not token: continue
        for oid in finance_full_access:
            _grant_one(T, token, typ, oid, "full_access")
        owner_transfer = _transfer_owner(T, token, typ, FINANCE_REPORT_OWNER)
        owners = _owners_for(T, fld)  # 渠道负责人(按职务实时查)
        for oid in owners:
            if oid in finance_full_access: continue
            _grant_one(T, token, typ, oid, "view")
        granted.append({"report": fld, "finance_full_access": len(finance_full_access),
                        "owner_transfer": owner_transfer, "channel_viewers": len(owners)})
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


NON_P0_COST_SOURCE_MARKERS = (
    "FBA盘点/退货冲抵",
)


def _company_cost_source_non_p0(source):
    s = ft(source)
    return any(marker in s for marker in NON_P0_COST_SOURCE_MARKERS)


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


def _company_period_month(value):
    raw = str(ft(value) or "")
    m = re.search(r"(20\d{2})[-/](\d{1,2})", raw)
    if not m:
        return ""
    month = int(m.group(2))
    if month < 1 or month > 12:
        return ""
    return f"{m.group(1)}-{month:02d}"


def _company_effective_report_month(period="", report_period=""):
    return _company_period_month(report_period) or _company_period_month(period)


def _company_funlabswitch_shopify_period(period="", report_period=""):
    month = _company_effective_report_month(period, report_period)
    return bool(month and month >= FUNLABSWITCH_SHOPIFY_CUTOFF)


def _company_platform_meta(platform_id, *, period="", report_period=""):
    key, base = _company_platform(platform_id)
    meta = dict(base)
    if key == "funlabswitch" and _company_funlabswitch_shopify_period(period, report_period):
        meta.update({
            "data_mode": "api",
            "data_status": "取数完成",
            "report_status": "待财务终审",
            "blocker_type": "",
            "blocker": "财务部",
            "maturity": "confirmed",
            "source_system": "Shopify Admin API",
            "generator_family": "shopify_admin_api",
            "generator_note": "2026-07 起 funlabswitch.com 迁移 Shopify，生成方式与 funlab.net/Powkong 统一；2026-06 仍保留历史 Shopline/成本缺口收口。",
        })
    return key, meta


def _company_apply_period_registry_override(platform_id, meta, *, period="", report_period=""):
    if platform_id == "funlabswitch" and _company_funlabswitch_shopify_period(period, report_period):
        meta.update(_company_platform_meta(platform_id, period=period, report_period=report_period)[1])
    return meta


def _company_run_id(period, platform_id):
    return f"company-profit-{period}-{platform_id}"


def _company_report_link(T, period, platform_id, *, report_period=None):
    lookup_period = ft(report_period or period)
    if "smoke" in lookup_period:
        return ""
    field = COMPANY_REPORT_FIELD_BY_PLATFORM.get(platform_id)
    if not field:
        return ""
    ym_slash = lookup_period.replace("-", "/")
    return _idx_links_all(T, ym_slash).get(field) or ""


def _company_generator_url(meta):
    meta = meta or {}
    explicit_env = ft(meta.get("generator_url_env"))
    if explicit_env and os.environ.get(explicit_env):
        return os.environ[explicit_env].rstrip("/")
    gtype = ft(meta.get("generator_type"))
    if gtype == "n8n_webhook":
        path = ft(meta.get("generator_path")).strip("/")
        return f"{N8N_WEBHOOK_BASE_URL}/webhook/{path}" if path else ""
    if gtype == "service_endpoint":
        base = (os.environ.get(ft(meta.get("service_base_url_env"))) or ft(meta.get("service_base_url"))).rstrip("/")
        endpoint = ft(meta.get("service_endpoint")).lstrip("/")
        return f"{base}/{endpoint}" if base and endpoint else ""
    return ""


def _company_domestic_summary_record_id(T, period):
    ym = ft(period).replace("/", "-")
    if not ym:
        return ""
    for rec in _bitable_all(T, DOMESTIC_ECOM_TASK_APP, DOMESTIC_ECOM_TASK_TBL):
        f = rec.get("fields", {})
        if ft(f.get("月份")).replace("/", "-") == ym and ft(f.get("数据类型")) == "月度报表汇总":
            return rec.get("record_id") or ""
    return ""


def _company_generator_record_id(payload, *, T=None, fields=None, meta=None):
    for key in ("source_record_id", "task_record_id", "generator_record_id"):
        value = ft((payload or {}).get(key))
        if value:
            return value
    meta = meta or {}
    if T and ft(meta.get("generator_lookup")) == "domestic_summary_record":
        period = ft((payload or {}).get("report_period")) or ft((fields or {}).get("期间"))
        return _company_domestic_summary_record_id(T, period)
    return ""


def _company_generator_status(fields, T=None):
    payload = _company_run_payload(fields)
    platform_id = ft(payload.get("platform_id"))
    period = ft((fields or {}).get("期间"))
    report_period = ft(payload.get("report_period")) or period
    registry_meta = _company_platform_meta(platform_id, period=period, report_period=report_period)[1] if platform_id else {}
    meta = dict(registry_meta)
    meta.update({k: v for k, v in (payload.get("meta") or {}).items() if v not in ("", None)})
    meta = _company_apply_period_registry_override(platform_id, meta, period=period, report_period=report_period)
    gtype = ft(meta.get("generator_type"))
    url = _company_generator_url(meta)
    required = ft(meta.get("generator_requires"))
    missing = []
    if not gtype:
        missing.append("generator_type")
    if gtype in ("n8n_webhook", "service_endpoint") and not url:
        missing.append("generator_url")
    record_id = _company_generator_record_id(payload, T=T, fields=fields, meta=meta)
    if required == "source_record_id" and not record_id:
        missing.append("source_record_id")
    if required == "seller_id" and not ft(payload.get("seller_id")):
        missing.append("seller_id")
    auth_env = ft(meta.get("auth_token_env"))
    if auth_env and not os.environ.get(auth_env):
        missing.append(auth_env)
    direct_types = {"n8n_webhook", "service_endpoint"}
    direct = gtype in direct_types and not missing
    allowed = platform_id in COMPANY_GENERATOR_ALLOWED_PLATFORMS
    if not gtype:
        reason = "此平台还未登记自动生成器。"
    elif missing:
        reason = "生成器还不能直接触发，缺少：" + ", ".join(missing)
    elif gtype not in direct_types:
        reason = ft(meta.get("generator_note")) or "此生成器不是公司级直接触发入口。"
    elif COMPANY_GENERATOR_ENABLED and not allowed:
        reason = "生成器总开关已打开，但此平台未在灰度白名单中。"
    else:
        reason = "生成器已登记，可在开关打开后触发。"
    return {
        "platform_id": platform_id,
        "type": gtype or "",
        "workflow_id": ft(meta.get("workflow_id")),
        "method": ft(meta.get("generator_method")) or "GET",
        "url": url,
        "requires": required,
        "source_record_id": record_id,
        "missing": missing,
        "direct": direct,
        "allowed": allowed,
        "enabled": COMPANY_GENERATOR_ENABLED,
        "ready": direct and COMPANY_GENERATOR_ENABLED and allowed,
        "note": ft(meta.get("generator_note")),
        "reason": reason,
    }


def _company_refresh_report_link(T, run):
    fields = (run or {}).get("fields", {})
    run_id = ft(fields.get("run_id"))
    payload = _company_run_payload(fields)
    platform_id = ft(payload.get("platform_id"))
    period = ft(fields.get("期间"))
    if not (run_id and platform_id and period):
        return run
    report_period = ft(payload.get("report_period")) or period
    link = _company_report_link(T, period, platform_id, report_period=report_period)
    if link and link != ft(fields.get("报表链接")):
        return _company_update_run(T, run_id, {"报表链接": link, "最后动作": "generator_refreshed_report_link"}) or run
    return run


def _company_iso_ms(value):
    text = ft(value)
    if not text:
        return 0
    try:
        return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _company_n8n_executions(workflow_id, limit=8):
    if not (N8N_API_KEY and workflow_id):
        return []
    r = requests.get(
        f"{N8N_API_BASE_URL}/executions",
        headers={"X-N8N-API-KEY": N8N_API_KEY},
        params={"workflowId": workflow_id, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("data") or data.get("items") or []


def _company_poll_n8n_execution(workflow_id, since_ms, *, timeout_sec=None):
    timeout_sec = timeout_sec or COMPANY_GENERATOR_POLL_TIMEOUT
    if not workflow_id:
        return {"ok": False, "status": "unavailable", "message": "workflow_id missing"}
    if not N8N_API_KEY:
        return {"ok": False, "status": "unavailable", "message": "N8N_API_KEY missing"}
    deadline = time.time() + timeout_sec
    last_seen = None
    since_floor = max(0, int(since_ms or 0) - 10000)
    while time.time() < deadline:
        try:
            executions = _company_n8n_executions(workflow_id)
            candidates = []
            for exe in executions:
                started_ms = _company_iso_ms(exe.get("startedAt"))
                if started_ms >= since_floor:
                    candidates.append((started_ms, exe))
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                exe = candidates[0][1]
                last_seen = {
                    "id": ft(exe.get("id")),
                    "status": ft(exe.get("status")),
                    "finished": bool(exe.get("finished")),
                    "startedAt": ft(exe.get("startedAt")),
                    "stoppedAt": ft(exe.get("stoppedAt")),
                }
                terminal = last_seen["finished"] or last_seen["status"] in ("success", "error", "crashed", "canceled", "cancelled")
                if terminal:
                    ok = last_seen["status"] == "success" or (last_seen["finished"] and last_seen["status"] not in ("error", "crashed", "canceled", "cancelled"))
                    return {**last_seen, "ok": ok, "message": "n8n execution finished"}
        except Exception as e:
            last_seen = {"status": "poll_error", "message": str(e)[:160]}
        time.sleep(COMPANY_GENERATOR_POLL_INTERVAL)
    return {"ok": False, "status": "timeout", "message": "n8n execution poll timeout", "last_seen": last_seen}


def _company_trigger_poll_status(status, started_ms):
    if status.get("type") != "n8n_webhook":
        return {"ok": False, "status": "unavailable", "message": "poll only supports n8n_webhook"}
    return _company_poll_n8n_execution(status.get("workflow_id"), started_ms)


def _company_trigger_generator(T, run, *, source_action="rerun"):
    fields = (run or {}).get("fields", {})
    run_id = ft(fields.get("run_id"))
    status = _company_generator_status(fields, T=T)
    before = {"run": fields, "generator": status}
    if not run_id:
        return {"ok": False, "status": "invalid", "message": "run_id missing", "generator": status, "run": run}
    if not status["direct"]:
        after = {"generator": status}
        _company_update_run(T, run_id, {"最后动作": f"{source_action}_generator_not_ready"})
        _company_write_system_audit(T, f"{source_action}_generator_not_ready", run_id,
                                    before, after, status, "skipped")
        return {"ok": False, "status": "not_ready", "message": status["reason"], "generator": status, "run": run}
    if not COMPANY_GENERATOR_ENABLED:
        after = {"generator": status}
        _company_update_run(T, run_id, {"最后动作": f"{source_action}_generator_skipped_disabled"})
        _company_write_system_audit(T, f"{source_action}_generator_skipped_disabled", run_id,
                                    before, after, status, "skipped")
        return {"ok": False, "status": "skipped_disabled", "message": "生成器已登记，但 COMPANY_GENERATOR_ENABLED=false，本次没有触发外部报表。", "generator": status, "run": run}
    if not status["allowed"]:
        after = {"generator": status}
        _company_update_run(T, run_id, {"最后动作": f"{source_action}_generator_skipped_not_allowed"})
        _company_write_system_audit(T, f"{source_action}_generator_skipped_not_allowed", run_id,
                                    before, after, status, "skipped")
        return {"ok": False, "status": "skipped_not_allowed", "message": "生成器总开关已打开，但此平台未在灰度白名单中，本次没有触发外部报表。", "generator": status, "run": run}

    payload = _company_run_payload(fields)
    period = ft(fields.get("期间"))
    report_period = ft(payload.get("report_period")) or period
    params = {"period": period, "month": report_period, "run_id": run_id, "source": source_action}
    headers = {}
    platform_id = ft(payload.get("platform_id"))
    meta = dict(_company_platform_meta(platform_id, period=period, report_period=report_period)[1] if platform_id else {})
    meta.update({k: v for k, v in (payload.get("meta") or {}).items() if v not in ("", None)})
    meta = _company_apply_period_registry_override(platform_id, meta, period=period, report_period=report_period)
    auth_env = ft(meta.get("auth_token_env"))
    if auth_env and os.environ.get(auth_env):
        headers["Authorization"] = f"Bearer {os.environ[auth_env]}"
    method = (status["method"] or "GET").upper()
    body = {}
    record_id = _company_generator_record_id(payload, T=T, fields=fields, meta=meta)
    if record_id:
        body["record_id"] = record_id

    started_ms = _now_ms()
    try:
        if method == "GET":
            resp = requests.get(status["url"], params=params, headers=headers, timeout=COMPANY_GENERATOR_REQUEST_TIMEOUT)
        else:
            resp = requests.request(method, status["url"], params=params, headers=headers,
                                    json=body or None, timeout=COMPANY_GENERATOR_REQUEST_TIMEOUT)
        text = resp.text[:1000]
        ok = 200 <= resp.status_code < 300
        run2 = _company_refresh_report_link(T, _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run)
        after = {"http_status": resp.status_code, "body": text, "run": (run2 or {}).get("fields", {})}
        _company_update_run(T, run_id, {"最后动作": f"{source_action}_generator_triggered" if ok else f"{source_action}_generator_failed"})
        _company_write_system_audit(T, f"{source_action}_generator_trigger", run_id,
                                    before, after, {**status, "params": params, "body": body}, "ok" if ok else "error")
        return {"ok": ok, "status": "triggered" if ok else "failed",
                "http_status": resp.status_code, "message": text[:160],
                "generator": status, "run": run2 or run}
    except Exception as e:
        poll = _company_trigger_poll_status(status, started_ms)
        run2 = _company_refresh_report_link(T, _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run)
        if poll.get("ok"):
            after = {"http_error": str(e)[:500], "poll": poll, "run": (run2 or {}).get("fields", {})}
            _company_update_run(T, run_id, {"最后动作": f"{source_action}_generator_triggered_async"})
            _company_write_system_audit(T, f"{source_action}_generator_trigger", run_id,
                                        before, after, {**status, "params": params, "body": body, "poll": poll}, "ok")
            return {"ok": True, "status": "triggered_async", "message": "n8n execution succeeded after async poll",
                    "generator": status, "poll": poll, "run": run2 or run}
        after = {"error": str(e)[:500], "poll": poll, "generator": status, "run": (run2 or {}).get("fields", {})}
        _company_update_run(T, run_id, {"最后动作": f"{source_action}_generator_error"})
        _company_write_system_audit(T, f"{source_action}_generator_error", run_id,
                                    before, after, {**status, "poll": poll}, "error")
        return {"ok": False, "status": "error", "message": str(e)[:160], "generator": status, "poll": poll, "run": run2 or run}


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


def _company_seed_run(T, period, platform_id, *, override=None, report_period=None):
    platform_id, meta = _company_platform_meta(platform_id, period=period, report_period=report_period)
    run_id = _company_run_id(period, platform_id)
    report_link = _company_report_link(T, period, platform_id, report_period=report_period)
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
        "报表链接": report_link,
        "最后动作": "seed_run",
        "最后动作时间": str(_now_ms()),
        "payload_json": _compact_json({"platform_id": platform_id, "meta": meta, "report_period": ft(report_period or period)}),
    }
    if override:
        fields.update({k: str(v) if isinstance(v, (int, float, bool)) else v for k, v in override.items()})
    _bt_write(T, COMPANY_RUN_TBL, fields, "run_id")
    return _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {"fields": fields}


def _company_create_gap(T, run_id, period, platform, gap_type, detail, *, p_level="P0", owner="", payload_extra=None):
    gap_id = "gap_" + hashlib.sha1(f"{run_id}:{gap_type}:{detail}".encode("utf-8")).hexdigest()[:14]
    payload = {"run_id": run_id, "gap_type": gap_type, "detail": detail}
    if payload_extra:
        payload.update(payload_extra)
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
        "payload_json": _compact_json(payload),
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


def _company_write_system_audit(T, action, run_id, before, after, payload, result, target_type="run", target_id=""):
    key = hashlib.sha256(
        f"system:{action}:{run_id}:{target_id or run_id}:{_now_ms()}:{_payload_hash(payload)}".encode("utf-8")
    ).hexdigest()[:32]
    _company_write_audit(T, key, action, "system", run_id, target_type, target_id or run_id,
                         before, after, payload, result)


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
             qty=colexact(hdr, "销量"), rq=colidx(hdr, "退货数量") or colidx(hdr, "退款数量"),
             cost_source=colidx(hdr, "成本来源"))
    a = dict(sales=0, margin=0, payback=0, cost=0, freight=0, ad=0, pf=0, qty=0, rq=0, cm_n=0, cm_amt=0)
    def g(row, i): return _aggnum(row[i]) if (i is not None and i < len(row)) else 0
    def text(row, i): return ft(row[i]) if (i is not None and i < len(row)) else ""
    for row in rows:
        rs = g(row, c["sales"]); rc = g(row, c["cost"])
        qty = g(row, c["qty"])
        refund_qty = g(row, c["rq"])
        net_qty = qty - refund_qty
        source_non_p0 = _company_cost_source_non_p0(text(row, c["cost_source"]))
        if rs > 0 and net_qty > 0 and abs(rc) < 0.005 and not source_non_p0:
            a["cm_n"] += 1; a["cm_amt"] += rs   # 成本缺失行(有净销量且采购=0)
        a["sales"] += rs; a["margin"] += g(row, c["margin"]); a["payback"] += g(row, c["payback"])
        a["cost"] += rc; a["freight"] += g(row, c["freight"]); a["ad"] += g(row, c["ad1"]) + g(row, c["ad2"])
        a["pf"] += g(row, c["commission"]) + g(row, c["deliver"]) + g(row, c["storage"]) + g(row, c["vat"]) + g(row, c["adj"])
        a["qty"] += qty; a["rq"] += refund_qty
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


def _company_aggregate_gate(T, ym_dash, report_field):
    platform_id = COMPANY_AGG_PLATFORM_BY_REPORT_FIELD.get(report_field)
    if not platform_id:
        return {"allow": False, "why": f"{report_field} 未登记公司运行台平台映射", "delete_stale": False}
    run_id = _company_run_id(ym_dash, platform_id)
    rec = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)
    if not rec:
        return {"allow": False, "run_id": run_id, "why": f"{run_id} 尚未进入公司运行台", "delete_stale": False}
    fields = rec.get("fields", {})
    report_status = ft(fields.get("报表状态"))
    total_status = ft(fields.get("总表状态"))
    if report_status not in COMPANY_AGG_APPROVED_STATUSES:
        return {
            "allow": False,
            "run_id": run_id,
            "why": f"等待财务终审: 报表状态={report_status or '-'}; 总表状态={total_status or '-'}",
            "delete_stale": True,
        }
    return {"allow": True, "run_id": run_id, "run": rec, "report_status": report_status, "total_status": total_status}


def _company_mark_aggregate_loaded(T, gate):
    run_id = gate.get("run_id")
    if not run_id:
        return None
    report_status = ft(gate.get("report_status"))
    fields = {"总表状态": "已灌总表", "最后动作": "aggregate_total_table_loaded"}
    if report_status == "财务通过":
        fields["报表状态"] = "已灌总表"
        fields["当前阻断方"] = "归档"
    return _company_update_run(T, run_id, fields)


def _company_calc_aggregate(T, url, ptype, ym_dash):
    token, typ = _parse_link(url)
    if ptype == "xb":
        if typ != "sheet" or not token:
            raise ValueError("跨境报表链接不是电子表格")
        return _agg_xb(T, token)
    if ptype == "ecom":
        if typ != "sheet" or not token:
            raise ValueError("国内电商报表链接不是电子表格")
        return _agg_ecom(T, token)
    if ptype == "ml":
        return _agg_ml(T, ym_dash)
    raise ValueError(f"未知报表类型: {ptype}")


def _company_aggregate_run(T, run_id, *, archive=False):
    run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)
    fields = (run or {}).get("fields", {})
    if not fields:
        return {"ok": False, "reason": f"run not found: {run_id}"}
    payload, cfg = _company_agg_config(fields)
    if not cfg:
        return {"ok": False, "run_id": run_id, "reason": "这个平台还没有接入统一汇总解析"}
    period = ft(payload.get("report_period")) or ft(fields.get("期间"))
    ym_dash = period.replace("/", "-")
    report_link = ft(fields.get("报表链接"))
    if not report_link:
        return {"ok": False, "run_id": run_id, "reason": "没有绑定毛利报表链接"}

    fld, cat, plat, shop, brand, ptype = cfg
    gate = _company_aggregate_gate(T, ym_dash, fld)
    if not gate.get("allow"):
        return {"ok": False, "run_id": run_id, "reason": gate.get("why"), "gate": "finance_not_approved"}
    try:
        a = _company_calc_aggregate(T, report_link, ptype, ym_dash)
    except Exception as e:
        return {"ok": False, "run_id": run_id, "reason": str(e)[:120]}
    if not a or a["sales"] <= 0:
        rm = _agg_delete(T, ym_dash, cat, plat, shop)
        return {"ok": False, "run_id": run_id, "reason": "数据缺漏:报表空/0销售", "removed_stale": rm}

    cm_amt = a.get("cm_amt", 0)
    pct = (cm_amt / a["sales"] * 100) if a["sales"] else 0
    if cm_amt > 0 and pct > GATE_PCT:
        rm = _agg_delete(T, ym_dash, cat, plat, shop)
        return {"ok": False, "run_id": run_id,
                "reason": f"审计未过:成本缺失{a['cm_n']}SKU ¥{round(cm_amt)}({pct:.1f}%>{GATE_PCT:.0f}%)",
                "removed_stale": rm}

    out_fields = _agg_fields(ym_dash, cat, plat, shop, brand, a, report_link, ptype)
    act = _agg_upsert(T, ym_dash, cat, plat, shop, out_fields)
    _company_mark_aggregate_loaded(T, gate)
    if archive:
        _company_update_run(T, run_id, {"报表状态": "已归档", "当前阻断方": "已完成",
                                        "总表状态": "已灌总表", "最后动作": "company_profit_archive_completed"})
    return {"ok": True, "run_id": run_id, "shop": shop, "act": act,
            "sales": round(a["sales"]), "margin": round(a["margin"]),
            "payback": round(a["payback"]), "archived": archive}


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
        gate = _company_aggregate_gate(T, ym_dash, fld)
        if not gate.get("allow"):
            rm = _agg_delete(T, ym_dash, cat, plat, shop) if gate.get("delete_stale") else False
            skipped.append({"shop": shop, "why": gate.get("why"), "gate": "finance_not_approved",
                            "run_id": gate.get("run_id"), "removed_stale": rm})
            continue
        try:
            a = _company_calc_aggregate(T, url, ptype, ym_dash)
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
        _company_mark_aggregate_loaded(T, gate)
        done.append({"shop": shop, "act": act, "sales": round(a["sales"]), "margin": round(a["margin"]), "payback": round(a["payback"]),
                     "cost_missing": (f"{a['cm_n']}SKU ¥{round(cm_amt)}" if a.get("cm_n") else None),
                     "run_id": gate.get("run_id")})
    # 总表授权(铁律①): 财务部全体可管理 + Frankie 可管理
    fin = _dept_members(T, FIN_DEPT)
    _grant_one(T, IDX_APP, "bitable", FRANKIE, "full_access")
    _grant_one(T, IDX_APP, "bitable", WXD, "full_access")
    _grant_one(T, IDX_APP, "bitable", FINANCE_REPORT_OWNER, "full_access")
    for oid in fin:
        if oid not in (FRANKIE, WXD, FINANCE_REPORT_OWNER): _grant_one(T, IDX_APP, "bitable", oid, "full_access")
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


def _company_link_button(text, url):
    return {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": "default", "url": url}


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


def _company_is_smoke_run(fields, run_id=""):
    period = ft(fields.get("期间"))
    return "smoke" in period or "smoke" in ft(run_id)


def _company_meta_from_run(fields):
    try:
        payload = json.loads(ft(fields.get("payload_json")) or "{}")
        meta = payload.get("meta") or {}
    except Exception:
        meta = {}
    channel = ft(fields.get("平台"))
    site = ft(meta.get("site")) or ft(meta.get("name"))
    if channel in ("funlabswitch", "funlab.net", "Powkong", "powkong"):
        site_map = {
            "funlabswitch": "funlabswitch.com",
            "funlab.net": "funlab.net",
            "Powkong": "powkong.com",
            "powkong": "powkong.com",
        }
        site = site_map.get(channel, site)
        channel = "独立站"
    return {"channel": channel or "-", "site": site or "-"}


def _company_mark_finance_approved(T, run_id, before_run):
    if _company_is_smoke_run(before_run, run_id):
        return {
            "报表状态": "已归档",
            "当前阻断方": "已完成",
            "总表状态": "已灌总表",
            "P0数量": "0",
            "最后动作": "company_profit_finance_approve_smoke_archived",
        }, "财务已确认通过；测试链路已写回为已灌总表、已归档。"
    return {
        "报表状态": "财务通过",
        "当前阻断方": "AI自动化",
        "总表状态": "待灌总表",
        "P0数量": "0",
        "最后动作": "company_profit_finance_approve",
    }, "财务已确认通过，下一步会写入公司总毛利表。"


def _company_run_payload(fields):
    try:
        return json.loads(ft(fields.get("payload_json")) or "{}")
    except Exception:
        return {}


def _company_agg_config(fields):
    payload = _company_run_payload(fields)
    platform_id = ft(payload.get("platform_id"))
    report_field = COMPANY_REPORT_FIELD_BY_PLATFORM.get(platform_id)
    for item in AGG_REPORTS:
        if item[0] == report_field:
            return payload, item
    return payload, None


def _company_sheet_row_count(T, token, ptype):
    if not token:
        return None
    try:
        prefer = "毛利结果" if ptype == "ecom" else None
        vals, _ = _sheet_vals(T, token, prefer=prefer)
        return len([r for r in vals[1:] if any(c not in (None, "") for c in r)]) if vals else None
    except Exception:
        return None


def _company_cost_gap_details_from_sheet(T, token, ptype, limit=30):
    if not token:
        return []
    prefer = "毛利结果" if ptype == "ecom" else None
    vals, _ = _sheet_vals(T, token, prefer=prefer)
    if not vals:
        return []
    hdr = vals[0]
    rows = vals[1:]
    c = {
        "owner": colidx(hdr, "Listing负责人") or colidx(hdr, "负责人"),
        "country": colexact(hdr, "国家") or colidx(hdr, "国家"),
        "shop": colexact(hdr, "店铺") or colidx(hdr, "店铺"),
        "msku": colidx(hdr, "MSKU") or colexact(hdr, "SKU") or colidx(hdr, "SKU"),
        "name": colidx(hdr, "中文名称") or colidx(hdr, "商品名称") or colidx(hdr, "品名"),
        "qty": colexact(hdr, "销量") or colidx(hdr, "销量"),
        "rq": colidx(hdr, "退货数量") or colidx(hdr, "退款数量"),
        "sales": colidx(hdr, "售价", "RMB") or colidx(hdr, "销售额", "RMB") or colidx(hdr, "净销售额"),
        "cost": colidx(hdr, "采购成本", "RMB") or colidx(hdr, "采购成本"),
        "freight": colidx(hdr, "头程", "RMB") or colidx(hdr, "物流成本"),
        "margin": colidx(hdr, "毛利润", "RMB") or colidx(hdr, "毛利额"),
        "netqty": colidx(hdr, "净销量"),
        "netsales": colidx(hdr, "净销售额"),
        "local_sku": colidx(hdr, "local_sku") or colidx(hdr, "ERP SKU"),
        "profit_cg": colidx(hdr, "利润报表cgPriceTotal"),
        "product_cg": colidx(hdr, "产品成本cg_price"),
        "erp_name": colidx(hdr, "ERP品名"),
        "cost_source": colidx(hdr, "成本来源"),
        "diagnosis": colidx(hdr, "诊断结论"),
    }

    def g(row, key):
        i = c.get(key)
        return _aggnum(row[i]) if (i is not None and i < len(row)) else 0

    def txt(row, key):
        i = c.get(key)
        return ft(row[i]) if (i is not None and i < len(row)) else ""

    details = []
    for idx, row in enumerate(rows, start=2):
        sales = g(row, "sales")
        if ptype == "ecom" and c.get("netqty") is not None:
            sales = g(row, "netsales") or sales
            is_gap = g(row, "netqty") > 0 and g(row, "cost") == 0
        elif c.get("qty") is not None:
            net_qty = g(row, "qty") - g(row, "rq")
            is_gap = net_qty > 0 and sales > 0 and abs(g(row, "cost")) < 0.005
        else:
            is_gap = sales > 0 and g(row, "cost") == 0
        if is_gap and _company_cost_source_non_p0(txt(row, "cost_source")):
            is_gap = False
        if not is_gap:
            continue
        details.append({
            "row": idx,
            "country": txt(row, "country"),
            "shop": txt(row, "shop"),
            "msku": txt(row, "msku"),
            "name": txt(row, "name"),
            "owner": txt(row, "owner"),
            "qty": round(g(row, "qty")),
            "sales_rmb": round(sales, 2),
            "purchase_cost_rmb": round(g(row, "cost"), 2),
            "freight_rmb": round(g(row, "freight"), 2),
            "margin_rmb": round(g(row, "margin"), 2),
            "local_sku": txt(row, "local_sku"),
            "profit_cg_price_total": round(g(row, "profit_cg"), 4),
            "product_cg_price": round(g(row, "product_cg"), 4),
            "erp_name": txt(row, "erp_name"),
            "cost_source": txt(row, "cost_source"),
            "diagnosis": txt(row, "diagnosis"),
        })
    return sorted(details, key=lambda x: -x["sales_rmb"])[:limit]


def _company_report_summary(T, fields):
    report_link = ft(fields.get("报表链接"))
    token, typ = _parse_link(report_link)
    payload, cfg = _company_agg_config(fields)
    period = ft(payload.get("report_period")) or ft(fields.get("期间"))
    ym_dash = period.replace("/", "-")
    result = {"ok": False, "link": report_link, "reason": "", "rows": None}
    if not report_link:
        result["reason"] = "没有绑定毛利报表链接"
        return result
    if not cfg:
        result["reason"] = "这个平台还没有接入统一汇总解析"
        return result
    fld, cat, plat, shop, brand, ptype = cfg
    try:
        if ptype == "xb":
            if typ != "sheet" or not token:
                raise ValueError("跨境报表链接不是电子表格")
            a = _agg_xb(T, token)
        elif ptype == "ecom":
            if typ != "sheet" or not token:
                raise ValueError("国内电商报表链接不是电子表格")
            a = _agg_ecom(T, token)
        elif ptype == "ml":
            a = _agg_ml(T, ym_dash)
        else:
            raise ValueError(f"未知报表类型: {ptype}")
        if not a:
            result["reason"] = "报表为空或读取失败"
            return result
        a = dict(a)
        a["gross_margin"] = (a.get("margin", 0) / a.get("sales", 0)) if a.get("sales") else 0
        result.update({"ok": True, "summary": a, "field": fld, "shop": shop,
                       "rows": _company_sheet_row_count(T, token, ptype),
                       "gap_details": _company_cost_gap_details_from_sheet(T, token, ptype, limit=200) if token else []})
        return result
    except Exception as e:
        result["reason"] = str(e)[:120]
        return result


def _company_money(v):
    return f"¥{_fmt(float(v or 0))}"


def _company_pct(v):
    return f"{float(v or 0) * 100:.1f}%"


def _company_gap_rows(T, run_id):
    rows = []
    for rec in _bitable_all(T, IDX_APP, COMPANY_GAP_TBL):
        f = rec.get("fields", {})
        if ft(f.get("run_id")) != run_id:
            continue
        rows.append({
            "level": ft(f.get("P级")),
            "type": ft(f.get("缺口责任类型")),
            "result": ft(f.get("处理结果")),
            "owner": ft(f.get("责任人")),
            "detail": ft(f.get("缺口说明")) or ft(f.get("证据")),
        })
    return rows


def _company_finance_audit_text(T, run_id, fields, report_summary):
    gaps = _company_gap_rows(T, run_id)
    open_p0 = [g for g in gaps if g["level"] == "P0" and g["result"] not in ("已补件", "确认例外", "已关闭")]
    exceptions = [g for g in gaps if g["level"] == "P0" and g["result"] == "确认例外"]
    summary = report_summary.get("summary") or {}
    lines = []
    if open_p0:
        lines.append(f"**AI初审：还有 {len(open_p0)} 个 P0 缺口未处理，不能终审通过。**")
        for g in open_p0[:3]:
            lines.append(f"- {GAP_TYPE_LABELS.get(g['type'], g['type'] or '缺口')}：{g['detail'][:80]}")
    elif exceptions:
        lines.append(f"**AI初审：P0 已确认例外 {len(exceptions)} 项，可以交财务判断是否接受例外。**")
        for g in exceptions[:3]:
            lines.append(f"- 例外：{g['detail'][:80]}")
    elif summary.get("cm_n"):
        lines.append(f"**AI初审：发现 {int(summary.get('cm_n') or 0)} 个成本缺口，涉及销售 {_company_money(summary.get('cm_amt'))}。**")
    elif report_summary.get("ok"):
        lines.append("**AI初审：未发现 P0 成本、物流或口径缺口。**")
    else:
        lines.append(f"**AI初审：暂时无法自动读取摘要。** 原因：{report_summary.get('reason') or '-'}")
    blocker_type = _company_label(GAP_TYPE_LABELS, fields.get("缺口责任类型"))
    blocker = ft(fields.get("当前阻断方"))
    if blocker_type != "-" or blocker not in ("", "-", "财务部"):
        lines.append(f"当前阻断信息：{blocker_type} / {blocker or '-'}")
    return "\n".join(lines)


def _company_finance_focus_text(report_summary):
    summary = report_summary.get("summary") or {}
    focus = [
        "请重点核对销售额、退款/退货、平台费/广告费、采购成本、物流成本和毛利率。",
        "如果存在未入账成本、特殊物流口径、异常退款或本月例外处理，请点“退回补缺口”。",
    ]
    if summary.get("sales", 0) <= 0 and report_summary.get("ok"):
        focus.insert(0, "这份报表销售额为 0，请确认本月是否确实无销售。")
    if summary.get("cm_n"):
        focus.insert(0, "报表仍有成本缺口，原则上不应终审通过，除非财务确认本月按例外处理。")
    return "\n".join(f"- {x}" for x in focus)


def _company_gap_detail_text(report_summary):
    details = report_summary.get("gap_details") or []
    if not details:
        return ""
    total = int((report_summary.get("summary") or {}).get("cm_n") or len(details))
    lines = [f"**具体成本缺口（共 {total} 条，按销售额从高到低）**",
             "行号=打开报表后左侧行号；财务可按行号或 MSKU 直接核对。"]
    shown = details[:20]
    for i, g in enumerate(shown, start=1):
        name = f"｜{g['name']}" if g.get("name") else ""
        owner = g.get("owner") or "-"
        shop = g.get("shop") or "-"
        msku = g.get("msku") or "-"
        source = f"｜{g.get('cost_source')}" if g.get("cost_source") else ""
        lines.append(f"{i}. 行{g['row']}｜{shop}｜{msku}{name}｜{owner}｜销售 {_company_money(g.get('sales_rmb'))}｜采购成本 0{source}")
    if total > len(shown):
        lines.append(f"还有 {total - len(shown)} 条未在卡片展开，请在报表中筛选采购成本=0。")
    return "\n".join(lines)


def _company_finance_block_reason(T, run_id, report_summary):
    open_p0 = _company_open_p0_count(T, run_id)
    if open_p0 > 0:
        return f"还有 {open_p0} 个 P0 问题未处理，不能进入财务终审通过。"
    summary = report_summary.get("summary") or {}
    cm_n = int(summary.get("cm_n") or 0)
    if cm_n > 0:
        return f"AI 初审发现 {cm_n} 个采购成本缺口，涉及销售 {_company_money(summary.get('cm_amt'))}。成本补齐或确认例外前，不能终审通过。"
    return ""


def _company_gap_payload(fields):
    try:
        return json.loads(ft((fields or {}).get("payload_json")) or "{}")
    except Exception:
        return {}


def _company_owner_gap_instruction(details):
    sources = {ft(g.get("cost_source")) for g in details}
    lines = []
    if "Listing未配对ERP SKU" in sources:
        lines += [
            "**需要你处理：补配 ERP SKU**",
            "1. 打开领星 Amazon Listing / SKU 配对页面。",
            "2. 按卡片里的 MSKU 搜索对应 Listing。",
            "3. 把 `local_sku / ERP SKU` 绑定到正确的领星 ERP SKU。",
            "4. 不确定 SKU 时，先核对产品库或找采购确认；不要用临时成本或随便配一个 SKU。",
        ]
    if "产品cg_price存在但利润报表为0" in sources:
        lines += [
            "**需要你核实：成本计价已重算，但利润报表成本还没带出来**",
            "这些行已经配到 ERP SKU，产品成本也存在，不是让你补产品成本。",
            "1. 先到领星 `财务 -> 成本计价`，选择对应月份和店铺，点击右上角 `重算`。",
            "2. 等成本计价显示重算完成后，再回到 `利润报表` 刷新/重算，确认采购成本已经带出来。",
            "3. 如果成本计价里有金额，但利润报表仍然为 0，先不要点通过，反馈给 AI/财务排查利润报表分摊或接口缓存。",
        ]
    if not lines:
        lines += [
            "**需要你处理：毛利报表成本缺口**",
            "请按报表里的诊断结论补齐 SKU 配对、产品成本或利润报表成本同步，再回到卡片点击确认。",
        ]
    lines.append("全部处理完以后，再点卡片按钮；系统会自动重新生成并初审，不需要你去 Base 手动改状态。")
    return "\n".join(lines)


def _company_owner_gap_detail_text(owner, details):
    total_sales = sum(float(g.get("sales_rmb") or 0) for g in details)
    lines = [
        f"AI 初审把 **{len(details)} 条**成本缺口定位到 **{owner or '待确认负责人'}**，涉及销售 {_company_money(total_sales)}。",
        "请按下面行号和 MSKU 逐条处理：",
    ]
    for i, g in enumerate(details[:20], start=1):
        sku = g.get("local_sku") or "未配对"
        product_cg = g.get("product_cg_price")
        cg_txt = "-" if product_cg in ("", None) else str(product_cg)
        src = g.get("cost_source") or g.get("diagnosis") or "待诊断"
        name = f"｜{g.get('name')}" if g.get("name") else ""
        country = f"｜{g.get('country')}" if g.get("country") else ""
        lines.append(
            f"{i}. 行{g.get('row')}｜{g.get('shop') or '-'}{country}｜{g.get('msku') or '-'}{name}"
            f"｜ERP SKU: {sku}｜产品cg_price: {cg_txt}｜{src}"
        )
    if len(details) > 20:
        lines.append(f"还有 {len(details) - 20} 条未在卡片展开，请打开报表筛选你的负责人和采购成本=0。")
    lines += ["", _company_owner_gap_instruction(details)]
    return "\n".join(lines)


def _company_sync_owner_cost_gaps(T, run, report_summary, *, source_action="audit_gate"):
    fields = (run or {}).get("fields", {})
    run_id = ft(fields.get("run_id"))
    if not run_id:
        return []
    details = report_summary.get("gap_details") or []
    grouped = defaultdict(list)
    for item in details:
        owner = ft(item.get("owner")) or "待确认负责人"
        grouped[owner].append(item)
    current_gap_ids = set()
    created = []
    period = ft(fields.get("期间"))
    ident = _company_meta_from_run(fields)
    for owner, rows in sorted(grouped.items(), key=lambda kv: (-sum(float(x.get("sales_rmb") or 0) for x in kv[1]), kv[0])):
        detail = _company_owner_gap_detail_text(owner, rows)
        gap = _company_create_gap(
            T, run_id, period, ident["channel"], "master_data_gap", detail,
            p_level="P0", owner=owner,
            payload_extra={"subtype": "owner_cost_gap", "owner": owner, "details": rows, "source_action": source_action},
        )
        gid = ft((gap.get("fields") or {}).get("gap_id"))
        if gid:
            current_gap_ids.add(gid)
        created.append(gap)

    for rec in _bitable_all(T, IDX_APP, COMPANY_GAP_TBL):
        f = rec.get("fields", {})
        if ft(f.get("run_id")) != run_id:
            continue
        if ft(f.get("P级")) != "P0" or ft(f.get("处理结果")) not in ("", "待处理"):
            continue
        payload = _company_gap_payload(f)
        gap_id = ft(f.get("gap_id"))
        detail = ft(f.get("缺口说明"))
        is_owner_gap = payload.get("subtype") == "owner_cost_gap"
        is_summary_gap = payload.get("subtype") == "summary_cost_gap" or ("AI初审发现" in detail and "采购成本缺口" in detail)
        if (is_owner_gap and gap_id not in current_gap_ids) or is_summary_gap:
            _company_update_gap(T, gap_id, {"处理结果": "已关闭", "最后动作": f"{source_action}_split_to_owner_gaps"})
    return created


def _company_apply_report_audit_gate(T, run, *, source_action="finance_card_report"):
    fields = (run or {}).get("fields", {})
    run_id = ft(fields.get("run_id"))
    if not run_id:
        return run
    report_summary = _company_report_summary(T, fields)
    summary = report_summary.get("summary") or {}
    cm_n = int(summary.get("cm_n") or 0)
    if cm_n <= 0:
        _company_sync_owner_cost_gaps(T, run, report_summary, source_action=source_action)
        return run
    period = ft(fields.get("期间"))
    ident = _company_meta_from_run(fields)
    detail = f"AI初审发现 {cm_n} 个采购成本缺口，涉及销售 {_company_money(summary.get('cm_amt'))}。成本补齐或确认例外前不能进入财务终审。"
    owner_gaps = _company_sync_owner_cost_gaps(T, run, report_summary, source_action=source_action)
    if not owner_gaps:
        gap_detail_text = _company_gap_detail_text(report_summary)
        if gap_detail_text:
            detail += "\n\n" + gap_detail_text
        _company_create_gap(T, run_id, period, ident["channel"], "master_data_gap", detail,
                            p_level="P0", owner="采购/负责人",
                            payload_extra={"subtype": "summary_cost_gap", "source_action": source_action})
    _company_update_run(T, run_id, {"报表状态": "P0待处理", "当前阻断方": "采购/负责人",
                                    "缺口责任类型": "master_data_gap",
                                    "P0数量": str(_company_open_p0_count(T, run_id)),
                                    "最后动作": f"{source_action}_audit_blocked"})
    return _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run


def _company_first_open_p0_gap(T, run_id):
    for rec in _bitable_all(T, IDX_APP, COMPANY_GAP_TBL):
        f = rec.get("fields", {})
        if ft(f.get("run_id")) != run_id:
            continue
        if ft(f.get("P级")) == "P0" and ft(f.get("处理结果")) not in ("已补件", "确认例外", "已关闭"):
            return rec
    return None


def _company_platform_ids(platform_param="", scope="v1", *, period=""):
    if platform_param:
        ids = [p.strip() for p in str(platform_param).split(",") if p.strip()]
    else:
        scope = (scope or "v1").strip()
        funlabswitch_is_ready = _company_funlabswitch_shopify_period(period)
        if scope == "all":
            ids = list(COMPANY_PLATFORM_REGISTRY.keys())
        elif scope == "p0":
            ids = [pid for pid in COMPANY_P0_PLATFORM_IDS if not (pid == "funlabswitch" and funlabswitch_is_ready)]
        elif scope == "p1":
            ids = list(COMPANY_P1_PLATFORM_IDS)
        elif scope == "p2":
            ids = list(COMPANY_P2_PLATFORM_IDS)
        elif scope == "ready":
            ids = list(COMPANY_V1_PLATFORM_IDS)
            if funlabswitch_is_ready and "funlabswitch" not in ids:
                ids.append("funlabswitch")
        else:
            ids = list(COMPANY_V1_PLATFORM_IDS)
            if funlabswitch_is_ready and "funlabswitch" not in ids:
                ids.append("funlabswitch")
    for pid in ids:
        _company_platform(pid)
    return ids


def _company_missing_report_gap_type(meta):
    mode = ft((meta or {}).get("data_mode"))
    if mode == "api":
        return "api_error", "AI自动化"
    if mode == "manual":
        return "source_file_gap", "平台负责人"
    if mode == "ledger":
        return "workflow_gap", "AI自动化"
    if mode == "hybrid":
        return "master_data_gap", "采购/负责人"
    return "workflow_gap", "AI自动化"


def _company_open_report_gap(T, run):
    fields = (run or {}).get("fields", {})
    run_id = ft(fields.get("run_id"))
    if not run_id:
        return run
    payload = _company_run_payload(fields)
    platform_id = ft(payload.get("platform_id"))
    period = ft(fields.get("期间"))
    report_period = ft(payload.get("report_period")) or period
    meta = dict(_company_platform_meta(platform_id, period=period, report_period=report_period)[1] if platform_id else {})
    meta.update({k: v for k, v in (payload.get("meta") or {}).items() if v not in ("", None)})
    meta = _company_apply_period_registry_override(platform_id, meta, period=period, report_period=report_period)
    ident = _company_meta_from_run(fields)
    gap_type, owner = _company_missing_report_gap_type(meta)
    if platform_id in COMPANY_P1_PLATFORM_IDS + COMPANY_P2_PLATFORM_IDS:
        detail = f"{ident['channel']} {period} 的毛利报表还没有接入统一生成、初审、终审和灌总表流程。"
    else:
        detail = f"{ident['channel']} {period} 没有绑定毛利报表链接，系统无法做 AI 初审或交给财务终审。"
    _company_create_gap(T, run_id, period, ident["channel"], gap_type, detail, p_level="P0", owner=owner)
    _company_update_run(T, run_id, {"报表状态": "P0待处理", "当前阻断方": owner,
                                    "缺口责任类型": gap_type,
                                    "P0数量": str(_company_open_p0_count(T, run_id)),
                                    "最后动作": "report_link_missing_blocked"})
    return _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run


def _company_audit_run_cycle(T, run, *, source_action="run_month"):
    before = {"run": (run or {}).get("fields", {})}
    fields = before["run"]
    run_id = ft(fields.get("run_id"))
    report_link = ft(fields.get("报表链接"))
    if not run_id:
        return {"ok": False, "run": run, "status": "invalid", "message": "run_id missing"}

    _company_update_run(T, run_id, {"报表状态": "AI初审中", "当前阻断方": "AI自动化",
                                    "最后动作": f"{source_action}_audit_started"})
    run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
    if not report_link and not _company_is_smoke_run(fields, run_id):
        run = _company_open_report_gap(T, run)
        after = {"run": (run or {}).get("fields", {}), "open_p0": _company_open_p0_count(T, run_id)}
        _company_write_system_audit(T, f"{source_action}_audit_blocked_no_report", run_id,
                                    before, after, {"source_action": source_action}, "blocked")
        return {"ok": False, "run": run, "status": "p0_gap", "open_p0": after["open_p0"],
                "gap": _company_first_open_p0_gap(T, run_id), "message": "没有绑定毛利报表链接"}

    run = _company_apply_report_audit_gate(T, run, source_action=source_action)
    open_p0 = _company_open_p0_count(T, run_id)
    if open_p0 == 0:
        _company_update_run(T, run_id, {"报表状态": "待财务终审", "当前阻断方": "财务部",
                                        "缺口责任类型": "", "P0数量": "0",
                                        "最后动作": f"{source_action}_audit_passed"})
        run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
        status, result, message = "finance_ready", "ok", "AI初审未发现未处理 P0，已进入待财务终审。"
    else:
        run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
        status, result, message = "p0_gap", "blocked", f"AI初审后仍有 {open_p0} 个 P0，不能进入财务终审。"
    after = {"run": (run or {}).get("fields", {}), "open_p0": open_p0}
    _company_write_system_audit(T, f"{source_action}_audit_cycle", run_id, before, after,
                                {"source_action": source_action}, result)
    return {"ok": open_p0 == 0, "run": run, "status": status, "open_p0": open_p0,
            "gap": _company_first_open_p0_gap(T, run_id), "message": message}


def _company_send_workflow_card(T, run, card_name, card, recipient_mode):
    ET = event_tok()
    run_id = ft((run or {}).get("fields", {}).get("run_id"))
    recipients = _company_finance_card_recipients(T, "frankie" if card_name == "p0_gap" else recipient_mode)
    sent = []
    for recipient in recipients:
        res = _send_event_card_union(ET, recipient["union_id"], card)
        mid = (res.get("data") or {}).get("message_id")
        if mid:
            _append_company_message_id(T, run_id, mid)
        sent.append({"to": recipient["name"], "code": res.get("code"), "message_id": mid})
    return sent


def _company_owner_gap_records(T, run_id):
    rows = []
    for rec in _bitable_all(T, IDX_APP, COMPANY_GAP_TBL):
        f = rec.get("fields", {})
        if ft(f.get("run_id")) != run_id:
            continue
        if ft(f.get("P级")) != "P0" or ft(f.get("处理结果")) not in ("", "待处理"):
            continue
        payload = _company_gap_payload(f)
        if payload.get("subtype") == "owner_cost_gap":
            rows.append(rec)
    return rows


def _company_send_owner_gap_cards(T, run, *, recipient_mode="frankie"):
    run_id = ft((run or {}).get("fields", {}).get("run_id"))
    ET = event_tok()
    sent = []
    for gap in _company_owner_gap_records(T, run_id):
        gf = gap.get("fields", {})
        owner = ft(gf.get("责任人")) or "待确认负责人"
        card = _company_owner_gap_card(run, gap, test_mode=(recipient_mode != "owners"))
        if recipient_mode == "owners":
            recipient = _company_recipient_by_name(T, owner)
            recipients = [recipient] if recipient else []
        else:
            recipients = [{"name": f"Frankie(代{owner})", "union_id": FRANKIE_UNION_ID}]
        if not recipients:
            sent.append({"owner": owner, "gap_id": ft(gf.get("gap_id")), "code": "no_recipient", "message_id": None})
            continue
        for recipient in recipients:
            res = _send_event_card_union(ET, recipient["union_id"], card)
            mid = (res.get("data") or {}).get("message_id")
            if mid:
                _append_company_message_id(T, run_id, mid)
            sent.append({"owner": owner, "to": recipient["name"], "gap_id": ft(gf.get("gap_id")),
                         "code": res.get("code"), "message_id": mid})
    return sent


def _company_rerun_run(T, run_id, *, send_next=False, recipient_mode="frankie",
                       source_action="rerun", trigger_generator=True):
    run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)
    if not run:
        return {"ok": False, "status": "not_found", "message": f"run not found: {run_id}"}
    _company_update_run(T, run_id, {"报表状态": "AI生成中", "当前阻断方": "AI自动化",
                                    "最后动作": f"{source_action}_rerun_started"})
    run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
    generator = _company_trigger_generator(T, run, source_action=source_action) if trigger_generator else {
        "status": "not_requested", "message": "本次未请求生成器触发。", "generator": _company_generator_status(run.get("fields", {}), T=T), "run": run
    }
    run = generator.get("run") or _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
    result = _company_audit_run_cycle(T, run, source_action=source_action)
    run = result.get("run") or run
    card_name = None
    card = None
    if result.get("status") == "finance_ready":
        card_name = "finance"
        card = _company_finance_card(T, run, test_mode=(recipient_mode == "frankie"),
                                     test_note="Frankie-only 重跑验证卡；不会自动通知财务。" if recipient_mode == "frankie" else None)
    elif result.get("gap"):
        card_name = "p0_gap"
        card = _company_gap_card(run, result["gap"], test_mode=(recipient_mode == "frankie"))
    sent = _company_send_workflow_card(T, run, card_name, card, recipient_mode) if (send_next and card) else []
    result.update({"next_card": card_name, "sent": sent, "generator": generator})
    return result


def _company_async_job(run_id):
    with _COMPANY_ASYNC_LOCK:
        return dict(_COMPANY_ASYNC_JOBS.get(run_id) or {})


def _company_set_async_job(run_id, **updates):
    with _COMPANY_ASYNC_LOCK:
        job = dict(_COMPANY_ASYNC_JOBS.get(run_id) or {})
        job.update(updates)
        job["run_id"] = run_id
        _COMPANY_ASYNC_JOBS[run_id] = job
        return dict(job)


def _company_async_worker(run_id, *, send_next=False, recipient_mode="frankie", source_action="async_generate"):
    _company_set_async_job(run_id, status="running", started_at=str(_now_ms()), source_action=source_action)
    try:
        T = tok()
        result = _company_rerun_run(T, run_id, send_next=send_next, recipient_mode=recipient_mode,
                                    source_action=source_action, trigger_generator=True)
        generator = result.get("generator") or {}
        job = _company_set_async_job(
            run_id,
            status=result.get("status") or "done",
            ok=bool(result.get("ok")),
            open_p0=result.get("open_p0"),
            next_card=result.get("next_card"),
            generator_status=generator.get("status"),
            generator_message=generator.get("message"),
            finished_at=str(_now_ms()),
        )
        return job
    except Exception as e:
        msg = str(e)[:300]
        _company_set_async_job(run_id, status="error", ok=False, error=msg, finished_at=str(_now_ms()))
        try:
            T = tok()
            before = {"run_id": run_id}
            _company_update_run(T, run_id, {"最后动作": f"{source_action}_async_error"})
            _company_write_system_audit(T, f"{source_action}_async_error", run_id,
                                        before, {"error": msg}, {"source_action": source_action}, "error")
        except Exception:
            pass
        return None


def _company_start_async_run(run_id, *, send_next=False, recipient_mode="frankie", source_action="async_generate"):
    job = _company_set_async_job(run_id, status="queued", queued_at=str(_now_ms()),
                                 source_action=source_action, recipient_mode=recipient_mode,
                                 send_next=bool(send_next))
    th = threading.Thread(target=_company_async_worker,
                          kwargs={"run_id": run_id, "send_next": send_next,
                                  "recipient_mode": recipient_mode, "source_action": source_action},
                          daemon=True)
    th.start()
    return job


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
    ident = _company_meta_from_run(rf)
    platform = ident["channel"]
    site = ident["site"]
    gap_type = ft(gf.get("缺口责任类型")) or ft(rf.get("缺口责任类型"))
    detail = ft(gf.get("缺口说明")) or ft(gf.get("证据"))
    blocker = ft(rf.get("当前阻断方")) or ft(gf.get("责任人")) or "-"
    card_id = _company_card_id("p0_gap", run_id, gap_id)
    nonce = str(_now_ms())
    note = "测试卡，仅发给 Frankie；不会通知运营或财务。" if test_mode else "请确认后再点击，处理结果会自动更新在这张卡片上。"
    return _company_base_card("🔴 [FIN·P0] 毛利缺口待处理", "red", [
        _company_fields([
            ("渠道", platform),
            ("站点/店铺", site),
            ("月份", _company_period_label(period)),
            ("需要处理的人", blocker),
        ]),
        {"tag": "hr"},
        _company_md(
            f"**现在卡在哪里？**\n{detail or '有商品成本还没补齐，毛利会算不准。'}\n\n"
            "成本问题处理前，这份报表不会交给财务做最终确认，也不会写入公司总毛利表。"
        ),
        {"tag": "hr"},
        {"tag": "action", "actions": [
            _company_button("成本已补齐",
                            _company_payload("company_profit_gap_resolved", run_id, "p0_gap", card_id,
                                             gap_id=gap_id, platform=platform, period=period, nonce=nonce),
                            "primary"),
            _company_button("本月先按例外处理",
                            _company_payload("company_profit_gap_exception", run_id, "p0_gap", card_id,
                                             gap_id=gap_id, platform=platform, period=period,
                                             decision="exception", nonce=nonce)),
        ]},
        _company_note(note),
    ])


def _company_owner_gap_card(run, gap, *, test_mode=False):
    rf = run.get("fields", {})
    gf = gap.get("fields", {})
    run_id = ft(rf.get("run_id"))
    gap_id = ft(gf.get("gap_id"))
    period = ft(rf.get("期间")) or ft(gf.get("期间"))
    ident = _company_meta_from_run(rf)
    platform = ident["channel"]
    site = ident["site"]
    owner = ft(gf.get("责任人")) or "待确认负责人"
    detail = ft(gf.get("缺口说明")) or ft(gf.get("证据"))
    card_id = _company_card_id("owner_gap", run_id, gap_id)
    nonce = str(_now_ms())
    note = "测试卡，仅发给 Frankie；不会通知运营。" if test_mode else "处理完再点击；系统会自动重跑并再次初审。"
    return _company_base_card("🔴 [FIN·P0] 毛利缺口需处理", "red", [
        _company_fields([
            ("渠道", platform),
            ("站点/店铺", site),
            ("月份", _company_period_label(period)),
            ("需要处理的人", owner),
        ]),
        {"tag": "hr"},
        _company_md(detail or "这份报表里有商品成本还没补齐，毛利会算不准。"),
        {"tag": "hr"},
        _company_md("成本问题处理前，这份报表不会交给财务做最终确认，也不会写入公司总毛利表。"),
        {"tag": "hr"},
        {"tag": "action", "actions": [
            _company_button("我已处理，重跑初审",
                            _company_payload("company_profit_gap_resolved", run_id, "owner_gap", card_id,
                                             gap_id=gap_id, platform=platform, period=period,
                                             decision="owner_resolved", nonce=nonce),
                            "primary"),
        ]},
        _company_note(note),
    ])


def _company_finance_card(T, run, *, test_mode=False, test_note=None):
    rf = run.get("fields", {})
    run_id = ft(rf.get("run_id"))
    period = ft(rf.get("期间"))
    report_link = ft(rf.get("报表链接"))
    ident = _company_meta_from_run(rf)
    platform = ident["channel"]
    site = ident["site"]
    card_id = _company_card_id("finance_confirm", run_id, platform)
    nonce = str(_now_ms())
    note = (test_note or "测试卡，仅发给 Frankie；不会通知财务。") if test_mode else "请确认后再点击，处理结果会自动更新在这张卡片上。"
    report_summary = _company_report_summary(T, rf)
    summary = report_summary.get("summary") or {}
    block_reason = _company_finance_block_reason(T, run_id, report_summary)
    elements = [
        _company_fields([
            ("渠道", platform),
            ("站点/店铺", site),
            ("月份", _company_period_label(period)),
            ("目前状态", ft(rf.get("报表状态"))),
        ]),
        {"tag": "hr"},
        _company_md("请财务确认：这份毛利报表是否可以作为本月最终版本。"),
        {"tag": "hr"},
    ]
    if report_summary.get("ok"):
        elements += [
            _company_fields([
                ("销售额", _company_money(summary.get("sales"))),
                ("毛利润", _company_money(summary.get("margin"))),
                ("毛利率", _company_pct(summary.get("gross_margin"))),
                ("采购成本", _company_money(summary.get("cost"))),
                ("物流成本", _company_money(summary.get("freight"))),
                ("广告/平台费", _company_money((summary.get("ad") or 0) + (summary.get("pf") or 0))),
                ("销量", _fmt(summary.get("qty") or 0)),
                ("明细行数", report_summary.get("rows") if report_summary.get("rows") is not None else "-"),
            ]),
            {"tag": "hr"},
        ]
    else:
        elements += [_company_md(f"**报表数据摘要**\n暂时没有读到摘要：{report_summary.get('reason') or '-'}"), {"tag": "hr"}]
    elements += [
        _company_md(_company_finance_audit_text(T, run_id, rf, report_summary)),
        {"tag": "hr"},
    ]
    gap_detail_text = _company_gap_detail_text(report_summary)
    if gap_detail_text:
        elements += [_company_md(gap_detail_text), {"tag": "hr"}]
    elements += [
        _company_md("**财务重点看这里**\n" + _company_finance_focus_text(report_summary)),
        {"tag": "hr"},
    ]
    if report_link:
        elements.append({"tag": "action", "actions": [_company_link_button("打开毛利报表", report_link)]})
        elements.append({"tag": "hr"})
    else:
        link_msg = "测试卡未绑定真实毛利报表链接，当前只验证收件、按钮和写回。" if test_mode else "当前卡片还没有绑定毛利报表链接，请先补链接后再确认。"
        elements.append(_company_md(link_msg))
        elements.append({"tag": "hr"})
    if block_reason:
        elements += [
            _company_md("**当前不能终审通过**\n" + block_reason),
            {"tag": "hr"},
            {"tag": "action", "actions": [
                _company_button("退回补缺口",
                                _company_payload("company_profit_finance_return", run_id, "finance_confirm", card_id,
                                                 platform=platform, period=period, decision="return_p0", nonce=nonce),
                                "primary"),
            ]},
            _company_note(note),
        ]
        return _company_base_card("🔴 [FIN·P0] 毛利报表终审需先处理", "red", elements)
    elements += [
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
    ]
    return _company_base_card("🟡 [FIN·P2] 毛利报表终审", "orange", elements)


def _company_finance_card_recipients(T, mode):
    recipients = [{"name": "Frankie", "union_id": FRANKIE_UNION_ID}]
    if mode != "finance_gray":
        return recipients
    fin = _dept_members(T, FIN_DEPT)
    candidates = {"吴晓丹": WXD, **{name: oid for oid, name in fin.items()}}
    seen = {FRANKIE_UNION_ID}
    for name, oid in candidates.items():
        union_id = _union_id_for_open_id(T, oid)
        if not union_id or union_id in seen:
            continue
        seen.add(union_id)
        recipients.append({"name": name or oid, "union_id": union_id})
    return recipients


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
    card_type = str(value.get("card_type") or "")
    if _company_audit_exists(T, idempotency_key):
        current_run = (_bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {}).get("fields", {})
        current_gap = (_bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id) or {}).get("fields", {}) if gap_id else {}
        current_ident = _company_meta_from_run(current_run)
        card = _company_processed_card("✅ 毛利卡片已处理", "这次点击已经记录过，重复点击不会再次改变状态。",
                                       details={"渠道": current_ident["channel"],
                                                "站点/店铺": current_ident["site"],
                                                "月份": _company_period_label(current_run.get("期间")),
                                                "当前状态": ft(current_run.get("报表状态")),
                                                "缺口状态": ft(current_gap.get("处理结果"))})
        return {"duplicate": True, "patch": _patch_or_fallback(ctx, card)}

    before = {"run": (_bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {}).get("fields", {}),
              "gap": (_bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id) or {}).get("fields", {}) if gap_id else {}}
    ok = True
    target_type = "run"
    target_id = run_id
    result_message = "已处理。"
    aggregate_result = None

    if action == "company_profit_gap_resolved":
        target_type, target_id = "gap", gap_id
        _company_update_gap(T, gap_id, {"处理结果": "已补件", "是否可进财务终审": "true",
                                        "来源message_id": ctx["message_id"], "最后动作": action})
        _company_update_run(T, run_id, {"报表状态": "待AI重跑", "当前阻断方": "AI自动化",
                                        "P0数量": str(_company_open_p0_count(T, run_id)), "最后动作": action})
        rerun = _company_rerun_run(T, run_id, send_next=COMPANY_CALLBACK_SEND_NEXT,
                                   recipient_mode="frankie", source_action="gap_resolved_callback")
        prefix = "已记录：这批缺口已处理。" if card_type == "owner_gap" else "已记录：成本已补齐。"
        if rerun.get("status") == "finance_ready":
            result_message = f"{prefix} 系统已重新初审，当前没有未处理 P0，已进入待财务终审。"
        elif rerun.get("open_p0"):
            result_message = f"{prefix} 系统已重新初审；当前仍有 {rerun.get('open_p0')} 个 P0 需要继续处理。"
        else:
            result_message = f"{prefix} 系统已触发重新初审。"
    elif action == "company_profit_gap_exception":
        target_type, target_id = "gap", gap_id
        _company_update_gap(T, gap_id, {"处理结果": "确认例外", "是否可进财务终审": "true",
                                        "来源message_id": ctx["message_id"], "最后动作": action})
        open_p0 = _company_open_p0_count(T, run_id)
        _company_update_run(T, run_id, {"报表状态": "待财务终审" if open_p0 == 0 else "P0待处理",
                                        "当前阻断方": "财务部" if open_p0 == 0 else "负责人/采购/财务",
                                        "P0数量": str(open_p0), "最后动作": action})
        result_message = "已记录：本月先按例外处理。系统会检查是否还有其他未处理问题。"
    elif action == "company_profit_finance_approve":
        open_p0 = _company_open_p0_count(T, run_id)
        before_run = before.get("run") or {}
        report_summary = _company_report_summary(T, before_run)
        block_reason = _company_finance_block_reason(T, run_id, report_summary)
        if open_p0 > 0:
            ok = False
            result_message = f"还有 {open_p0} 个关键问题没处理完，这次不能确认通过。"
        elif block_reason:
            ok = False
            result_message = block_reason
        elif not _company_is_smoke_run(before_run, run_id) and not ft(before_run.get("报表链接")):
            ok = False
            result_message = "这张终审卡还没有绑定毛利报表链接，不能确认通过。"
        else:
            update_fields, result_message = _company_mark_finance_approved(T, run_id, before_run)
            _company_update_run(T, run_id, update_fields)
            if not _company_is_smoke_run(before_run, run_id):
                aggregate_result = _company_aggregate_run(T, run_id, archive=True)
                if aggregate_result.get("ok"):
                    result_message = f"{result_message} 已写入公司总毛利表并完成归档。"
                else:
                    ok = False
                    result_message = f"{result_message} 但自动灌总表未完成：{aggregate_result.get('reason')}"
    elif action == "company_profit_finance_return":
        _company_update_run(T, run_id, {"报表状态": "P0待处理", "当前阻断方": "财务部",
                                        "P0数量": str(max(1, _company_open_p0_count(T, run_id))),
                                        "最后动作": action})
        result_message = "已退回处理问题，暂时不会写入公司总毛利表。"
    else:
        ok = False
        result_message = "这张卡片的按钮动作无法识别，请联系 AI 自动化处理。"

    after = {"run": (_bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or {}).get("fields", {}),
             "gap": (_bt_find(T, COMPANY_GAP_TBL, "gap_id", gap_id) or {}).get("fields", {}) if gap_id else {}}
    callback_payload = {"value": value, "form_value": ctx["form_value"]}
    if aggregate_result is not None:
        callback_payload["aggregate_result"] = aggregate_result
    _company_write_audit(T, idempotency_key, action, ctx["operator_open_id"], run_id, target_type, target_id,
                         before, after, callback_payload,
                         "ok" if ok else "blocked", ctx["message_id"])
    after_run = after.get("run") or {}
    after_gap = after.get("gap") or {}
    after_ident = _company_meta_from_run(after_run)
    result_details = {
        "渠道": after_ident["channel"],
        "站点/店铺": after_ident["site"],
        "月份": _company_period_label(after_run.get("期间")),
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


@app.get("/profit-workflow/generators")
async def profit_workflow_generators(request: Request):
    """Read-only generator registry audit. Does not trigger any external workflow."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    period = q.get("period") or _last_month()
    scope = q.get("scope") or "v1"
    platform_param = q.get("platform") or ""
    platform_ids = _company_platform_ids(platform_param, scope, period=period)
    T = tok()
    rows = []
    for pid in platform_ids:
        _, meta = _company_platform_meta(pid, period=period, report_period=period)
        fields = {"payload_json": _compact_json({"platform_id": pid, "meta": meta, "report_period": period})}
        rows.append({
            "platform": pid,
            "name": meta.get("name"),
            "data_mode": meta.get("data_mode"),
            "maturity": meta.get("maturity", ""),
            "generator": _company_generator_status(fields, T=T),
        })
    return {"period": period, "scope": scope, "generator_enabled": COMPANY_GENERATOR_ENABLED,
            "allowed_platforms": sorted(COMPANY_GENERATOR_ALLOWED_PLATFORMS),
            "n8n_webhook_base": N8N_WEBHOOK_BASE_URL, "platforms": rows}


@app.post("/profit-workflow/run-month")
async def profit_workflow_run_month(request: Request):
    """Production-oriented monthly orchestrator: seed runs, run AI initial audit, then route P0 or finance cards."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    period = q.get("period") or _last_month()
    report_period = q.get("report_period")
    scope = q.get("scope") or "v1"
    platform_param = q.get("platform") or ""
    recipient_mode = q.get("recipient_mode") or "frankie"
    send = q.get("send") == "true"
    include_cards = q.get("include_cards") == "true"
    generate = q.get("generate") == "true"
    async_generate = q.get("async") == "true" or q.get("async_generate") == "true"
    if recipient_mode not in ("frankie", "finance_gray"):
        raise HTTPException(400, "recipient_mode must be frankie or finance_gray")

    T = tok()
    platform_ids = _company_platform_ids(platform_param, scope, period=report_period or period)
    results = []
    cards = {}
    for pid in platform_ids:
        run = _company_seed_run(T, period, pid, report_period=report_period)
        run_id = ft((run or {}).get("fields", {}).get("run_id"))
        generator_status = _company_generator_status((run.get("fields") or {}), T=T)
        if generate and async_generate and generator_status.get("ready"):
            before = {"run": (run or {}).get("fields", {}), "generator": generator_status}
            run = _company_update_run(T, run_id, {"报表状态": "AI生成中", "当前阻断方": "AI自动化",
                                                 "最后动作": "run_month_generator_async_started"}) or run
            _company_write_system_audit(T, "run_month_generator_async_started", run_id,
                                        before, {"run": (run or {}).get("fields", {})},
                                        {"source_action": "run_month_async", "generator": generator_status}, "ok")
            job = _company_start_async_run(run_id, send_next=send, recipient_mode=recipient_mode,
                                           source_action="run_month_async")
            results.append({
                "platform": pid,
                "run_id": run_id,
                "status": "generator_async_started",
                "open_p0": _company_open_p0_count(T, run_id),
                "next_card": None,
                "message": "生成器已转后台执行；请用 /profit-workflow/poll-run 查询或补账。",
                "generator": {"status": "async_started", "message": "background generator job started",
                              "ready": generator_status.get("ready"), "allowed": generator_status.get("allowed"),
                              "type": generator_status.get("type"),
                              "workflow_id": generator_status.get("workflow_id")},
                "async_job": job,
                "sent": [],
                "report_link": ft((run.get("fields") or {}).get("报表链接")),
            })
            continue
        generator = _company_trigger_generator(T, run, source_action="run_month") if generate else {
            "status": "not_requested", "message": "本次月度编排未请求生成器触发。", "generator": generator_status, "run": run
        }
        run = generator.get("run") or run
        audit = _company_audit_run_cycle(T, run, source_action="run_month")
        run = audit.get("run") or run
        card_name = None
        card = None
        if audit.get("status") == "p0_gap":
            gap = audit.get("gap")
            if gap:
                card_name = "p0_gap"
                card = _company_gap_card(run, gap, test_mode=(recipient_mode == "frankie"))
        elif audit.get("status") == "finance_ready":
            card_name = "finance"
            card = _company_finance_card(T, run, test_mode=(recipient_mode == "frankie"),
                                         test_note="Frankie-only 月度编排测试卡；不会自动通知财务。" if recipient_mode == "frankie" else None)
        sent = _company_send_workflow_card(T, run, card_name, card, recipient_mode) if (send and card) else []
        if include_cards and card:
            cards[pid] = card
        results.append({
            "platform": pid,
            "run_id": run_id,
            "status": audit.get("status"),
            "open_p0": audit.get("open_p0", _company_open_p0_count(T, run_id)),
            "next_card": card_name,
            "message": audit.get("message"),
            "generator": {"status": generator.get("status"), "message": generator.get("message"),
                          "ready": (generator.get("generator") or {}).get("ready"),
                          "allowed": (generator.get("generator") or {}).get("allowed"),
                          "type": (generator.get("generator") or {}).get("type"),
                          "workflow_id": (generator.get("generator") or {}).get("workflow_id")},
            "sent": sent,
            "report_link": ft((run.get("fields") or {}).get("报表链接")),
        })
    return {"period": period, "report_period": report_period or period, "scope": scope,
            "platforms": platform_ids, "recipient_mode": recipient_mode, "generate": generate,
            "async_generate": async_generate, "send": send, "results": results,
            "card_template": {"schema": COMPANY_CARD_SCHEMA, "version": COMPANY_CARD_TEMPLATE_VERSION},
            "cards": cards if include_cards else {},
            "ledger": {"run_table": COMPANY_RUN_TBL, "gap_table": COMPANY_GAP_TBL, "audit_table": COMPANY_AUDIT_TBL}}


@app.post("/profit-workflow/rerun")
async def profit_workflow_rerun(request: Request):
    """Rerun one company-profit run after P0补件/例外处理; optionally send the next Frankie-only/gray card."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    run_id = q.get("run_id")
    if not run_id:
        period = q.get("period") or _last_month()
        platform_id = q.get("platform")
        if not platform_id:
            raise HTTPException(400, "run_id or platform is required")
        run_id = _company_run_id(period, platform_id)
    recipient_mode = q.get("recipient_mode") or "frankie"
    if recipient_mode not in ("frankie", "finance_gray"):
        raise HTTPException(400, "recipient_mode must be frankie or finance_gray")
    generate = q.get("generate", "true") != "false"
    async_generate = q.get("async") == "true" or q.get("async_generate") == "true"
    T = tok()
    if generate and async_generate:
        run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)
        if not run:
            raise HTTPException(404, f"run not found: {run_id}")
        generator_status = _company_generator_status((run.get("fields") or {}), T=T)
        if generator_status.get("ready"):
            before = {"run": (run or {}).get("fields", {}), "generator": generator_status}
            run = _company_update_run(T, run_id, {"报表状态": "AI生成中", "当前阻断方": "AI自动化",
                                                 "最后动作": "manual_rerun_generator_async_started"}) or run
            _company_write_system_audit(T, "manual_rerun_generator_async_started", run_id,
                                        before, {"run": (run or {}).get("fields", {})},
                                        {"source_action": "manual_rerun_async", "generator": generator_status}, "ok")
            job = _company_start_async_run(run_id, send_next=q.get("send") == "true",
                                           recipient_mode=recipient_mode, source_action="manual_rerun_async")
            return {"run_id": run_id, "recipient_mode": recipient_mode, "send": q.get("send") == "true",
                    "generate": generate, "async_generate": async_generate,
                    "status": "generator_async_started", "open_p0": _company_open_p0_count(T, run_id),
                    "next_card": None, "sent": [],
                    "generator": {"status": "async_started", "message": "background generator job started",
                                  "ready": generator_status.get("ready"),
                                  "allowed": generator_status.get("allowed"),
                                  "type": generator_status.get("type"),
                                  "workflow_id": generator_status.get("workflow_id")},
                    "async_job": job,
                    "message": "生成器已转后台执行；请用 /profit-workflow/poll-run 查询或补账。",
                    "ledger": {"run_table": COMPANY_RUN_TBL, "gap_table": COMPANY_GAP_TBL, "audit_table": COMPANY_AUDIT_TBL}}
    result = _company_rerun_run(T, run_id, send_next=q.get("send") == "true",
                                recipient_mode=recipient_mode, source_action="manual_rerun",
                                trigger_generator=generate)
    if result.get("status") == "not_found":
        raise HTTPException(404, result.get("message"))
    return {"run_id": run_id, "recipient_mode": recipient_mode, "send": q.get("send") == "true",
            "generate": generate,
            "async_generate": async_generate,
            "status": result.get("status"), "open_p0": result.get("open_p0"),
            "next_card": result.get("next_card"), "sent": result.get("sent"),
            "generator": {"status": (result.get("generator") or {}).get("status"),
                          "message": (result.get("generator") or {}).get("message"),
                          "ready": ((result.get("generator") or {}).get("generator") or {}).get("ready"),
                          "allowed": ((result.get("generator") or {}).get("generator") or {}).get("allowed"),
                          "type": ((result.get("generator") or {}).get("generator") or {}).get("type"),
                          "workflow_id": ((result.get("generator") or {}).get("generator") or {}).get("workflow_id")},
            "message": result.get("message"),
            "ledger": {"run_table": COMPANY_RUN_TBL, "gap_table": COMPANY_GAP_TBL, "audit_table": COMPANY_AUDIT_TBL}}


@app.post("/profit-workflow/owner-gap-cards")
async def profit_workflow_owner_gap_cards(request: Request):
    """Split P0 cost gaps by report owner and optionally send operation cards."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    run_id = q.get("run_id")
    period = q.get("period") or _last_month()
    report_period = q.get("report_period")
    platform_id = q.get("platform") or "amazon"
    recipient_mode = q.get("recipient_mode") or "frankie"
    if recipient_mode not in ("frankie", "owners"):
        raise HTTPException(400, "recipient_mode must be frankie or owners")
    send = q.get("send") == "true"
    include_cards = q.get("include_cards") == "true"
    T = tok()
    run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) if run_id else None
    if not run:
        run = _company_seed_run(T, period, platform_id, report_period=report_period)
        run_id = ft((run or {}).get("fields", {}).get("run_id"))
    run = _company_refresh_report_link(T, run)
    before = {"run": (run or {}).get("fields", {})}
    run = _company_apply_report_audit_gate(T, run, source_action="owner_gap_cards")
    run_id = ft((run or {}).get("fields", {}).get("run_id")) or run_id
    owner_gaps = _company_owner_gap_records(T, run_id)
    sent = _company_send_owner_gap_cards(T, run, recipient_mode=recipient_mode) if send else []
    after = {"run": (run or {}).get("fields", {}), "owner_gap_count": len(owner_gaps), "sent": sent}
    _company_write_system_audit(T, "owner_gap_cards", run_id, before, after,
                                {"recipient_mode": recipient_mode, "send": send}, "ok")
    cards = {}
    if include_cards:
        for gap in owner_gaps:
            owner = ft((gap.get("fields") or {}).get("责任人")) or "待确认负责人"
            cards[owner] = _company_owner_gap_card(run, gap, test_mode=(recipient_mode != "owners"))
    return {"run_id": run_id, "period": ft((run.get("fields") or {}).get("期间")),
            "platform": platform_id, "recipient_mode": recipient_mode, "send": send,
            "owner_gap_count": len(owner_gaps),
            "open_p0": _company_open_p0_count(T, run_id),
            "sent": sent,
            "cards": cards if include_cards else {},
            "card_template": {"schema": COMPANY_CARD_SCHEMA, "version": COMPANY_CARD_TEMPLATE_VERSION},
            "ledger": {"run_table": COMPANY_RUN_TBL, "gap_table": COMPANY_GAP_TBL, "audit_table": COMPANY_AUDIT_TBL}}


@app.api_route("/profit-workflow/poll-run", methods=["GET", "POST"])
async def profit_workflow_poll_run(request: Request):
    """Poll one run after async generation. Optional reconcile=true refreshes report link and runs AI audit."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    run_id = q.get("run_id")
    if not run_id:
        period = q.get("period") or _last_month()
        platform_id = q.get("platform")
        if not platform_id:
            raise HTTPException(400, "run_id or platform is required")
        run_id = _company_run_id(period, platform_id)
    T = tok()
    run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id)
    if not run:
        raise HTTPException(404, f"run not found: {run_id}")
    reconcile = q.get("reconcile") == "true"
    audit = None
    if reconcile:
        run = _company_refresh_report_link(T, run)
        audit = _company_audit_run_cycle(T, run, source_action=q.get("source_action") or "manual_poll")
        run = audit.get("run") or run
    run = _bt_find(T, COMPANY_RUN_TBL, "run_id", run_id) or run
    fields = run.get("fields", {})
    return {
        "run_id": run_id,
        "async_job": _company_async_job(run_id),
        "reconcile": reconcile,
        "audit": {"status": audit.get("status"), "open_p0": audit.get("open_p0"),
                  "message": audit.get("message")} if audit else None,
        "run": {
            "报表状态": ft(fields.get("报表状态")),
            "P0数量": ft(fields.get("P0数量")),
            "当前阻断方": ft(fields.get("当前阻断方")),
            "缺口责任类型": ft(fields.get("缺口责任类型")),
            "总表状态": ft(fields.get("总表状态")),
            "报表链接": ft(fields.get("报表链接")),
            "最后动作": ft(fields.get("最后动作")),
            "最后动作时间": ft(fields.get("最后动作时间")),
        },
        "ledger": {"run_table": COMPANY_RUN_TBL, "gap_table": COMPANY_GAP_TBL, "audit_table": COMPANY_AUDIT_TBL},
    }


@app.post("/profit-workflow/test-cards")
async def profit_workflow_test_cards(request: Request):
    """P0 smoke: create Base ledger rows and optionally send sample cards to Frankie only."""
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    q = request.query_params
    period = q.get("period") or _last_month()
    report_period = q.get("report_period")
    platform_id = q.get("platform") or "funlabswitch"
    card_type = q.get("card_type") or "both"  # p0_gap / finance / both
    recipient_mode = q.get("recipient_mode") or "frankie"  # frankie / finance_gray
    if recipient_mode == "finance_gray" and card_type != "finance":
        raise HTTPException(400, "recipient_mode=finance_gray only supports card_type=finance")
    send = q.get("send") == "true"
    T = tok()
    run = _company_seed_run(T, period, platform_id, report_period=report_period)
    rf = run.get("fields", {})
    run_id = ft(rf.get("run_id"))
    platform = ft(rf.get("平台"))
    gap = None
    cards = {}
    if card_type in ("p0_gap", "both"):
        detail = "这份报表里有商品还没有采购成本。成本没补齐时，毛利会算不准。请先补齐成本；如果确认本月可以临时按例外处理，再点“本月先按例外处理”。"
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
        run = _company_apply_report_audit_gate(T, run)
        if _company_open_p0_count(T, run_id) > 0:
            if recipient_mode == "finance_gray" and send:
                raise HTTPException(409, "AI初审发现P0缺口，财务终审卡未发送；请先处理P0。")
            gap = _company_first_open_p0_gap(T, run_id)
            cards["p0_gap"] = _company_gap_card(run, gap or {"fields": {}}, test_mode=True)
        else:
            test_note = "测试卡，仅发给 Frankie、吴晓丹和财务部；不会影响真实报表。" if recipient_mode == "finance_gray" else None
            cards["finance"] = _company_finance_card(T, run, test_mode=True, test_note=test_note)

    sent = {}
    if send:
        ET = event_tok()
        recipients = _company_finance_card_recipients(T, recipient_mode)
        for name, card in cards.items():
            sent[name] = []
            target_recipients = _company_finance_card_recipients(T, "frankie" if name == "p0_gap" else recipient_mode)
            for recipient in target_recipients:
                res = _send_event_card_union(ET, recipient["union_id"], card)
                mid = (res.get("data") or {}).get("message_id")
                if mid:
                    _append_company_message_id(T, run_id, mid)
                sent[name].append({"to": recipient["name"], "code": res.get("code"), "message_id": mid})
    return {"period": period, "report_period": report_period or period, "platform": platform_id, "run_id": run_id, "frankie_only": recipient_mode == "frankie",
            "recipient_mode": recipient_mode,
            "card_template": {"schema": COMPANY_CARD_SCHEMA, "version": COMPANY_CARD_TEMPLATE_VERSION},
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
