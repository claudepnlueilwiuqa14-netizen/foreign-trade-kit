#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sourcing.py —— 供应链对接抓取 + 外贸利润计算器

一、抓取部分（1688 / 阿里国际站 / AliExpress 批发价）
    1688 与 Alibaba 均有强反爬；本工具按顺序尝试：requests -> 真浏览器(playwright)，
    哪一步都失败就如实输出"未取到"，绝不编造供应商价格。

二、利润计算器（本文件核心价值，完全可离线精确计算）
    输入采购价、重量/体积、售价、平台、渠道 -> 输出：
      FOB / CIF 成本、头程运费、平台佣金、FBA 配送费、关税、广告费、
      毛利、毛利率、ROI、盈亏平衡售价、各渠道比价

用法：
  python sourcing.py search "wireless earbuds" --source alibaba,1688 --max 10
  python sourcing.py calc --cost 42 --weight 0.18 --price 29.99 --platform amazon \
         --channel yunexpress --ad 0.12 --hs 8518.30
  python sourcing.py channels --weight 0.18 --country US     # 渠道比价
  python sourcing.py calc --interactive-json item.json
"""
import os
import re
import sys
import json
import time
import argparse
from urllib.parse import quote

PROXY = os.environ.get("TRADE_PROXY", "http://127.0.0.1:7897")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    import requests
except Exception:
    requests = None

# ============================================================================
# 一、真实数据：汇率（在线取，失败则用内置值并显式标注）
# ============================================================================
FALLBACK_FX = {"USDCNY": 7.15, "EURCNY": 7.75, "GBPCNY": 9.05,
               "JPYCNY": 0.047, "USD": 1.0}


def get_fx():
    """真取汇率；拿不到就用内置值，并在结果里标 source=fallback。"""
    endpoints = ["https://open.er-api.com/v6/latest/CNY",
                 "https://api.exchangerate.host/latest?base=CNY"]
    for u in endpoints:
        try:
            hdr = {"User-Agent": UA}
            if requests is not None:
                r = requests.get(u, headers=hdr, timeout=15,
                                 proxies={"http": PROXY, "https": PROXY})
                if r.status_code != 200:
                    continue
                j = r.json()
            else:
                import urllib.request
                req = urllib.request.Request(u, headers=hdr)
                op = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
                j = json.loads(op.open(req, timeout=15).read().decode())
            rates = j.get("rates") or {}
            if not rates.get("USD"):
                continue
            out = {"CNY": 1.0}
            for k in ("USD", "EUR", "GBP", "JPY"):
                if rates.get(k):
                    out[k.upper() + "CNY"] = round(1.0 / float(rates[k]), 6)
            out["_base"] = j.get("base_code") or j.get("base")
            out["_updated"] = j.get("time_last_update_utc") or j.get("date")
            out["_source"] = u.split("/")[2]
            return out
        except Exception:
            continue
    fx = dict(FALLBACK_FX)
    fx.update({"_source": "fallback", "_base": "CNY",
               "_updated": "内置值（在线汇率接口不可达）"})
    return fx


# ============================================================================
# 二、物流渠道费率（行业公开常见档位，标注：实际以货代报价为准）
# ============================================================================
# 结构：渠道 -> {计费方式, 首重价格, 续重价格, 挂号费, 时效区间(天), 适用, 国家附加}
CHANNELS = {
    "china_post": {"name": "中国邮政挂号小包", "mode": "weight",
                   "first": 22.0, "add": 68.0, "unit": "kg", "reg": 8.0,
                   "eta": (18, 40), "track": "有", "limit": 2.0,
                   "note": "最便宜，时效慢，轻小件首选"},
    "yanwen": {"name": "燕文航空挂号小包", "mode": "weight",
               "first": 20.0, "add": 62.0, "unit": "kg", "reg": 9.0,
               "eta": (12, 25), "track": "有", "limit": 2.0,
               "note": "性价比高，2kg 内小件主力渠道"},
    "yunexpress": {"name": "云途专线(带电)", "mode": "weight",
                   "first": 35.0, "add": 55.0, "unit": "kg", "reg": 12.0,
                   "eta": (8, 15), "track": "有", "limit": 30.0,
                   "note": "带电产品可走，时效稳定，外贸主力"},
    "epacket": {"name": "e邮宝", "mode": "weight",
                "first": 25.0, "add": 60.0, "unit": "kg", "reg": 7.0,
                "eta": (10, 20), "track": "有", "limit": 2.0,
                "note": "平台认可度高，适合 2kg 内"},
    "ems": {"name": "EMS 国际快递", "mode": "weight",
            "first": 120.0, "add": 75.0, "unit": "kg", "reg": 0.0,
            "eta": (7, 15), "track": "有", "limit": 30.0,
            "note": "通达国家多，清关能力较强"},
    "dhl": {"name": "DHL 国际快递", "mode": "weight",
            "first": 210.0, "add": 95.0, "unit": "kg", "reg": 0.0,
            "eta": (3, 7), "track": "有", "limit": 70.0,
            "note": "最快，价格高，急件/样品"},
    "ups": {"name": "UPS 国际快递", "mode": "weight",
            "first": 195.0, "add": 90.0, "unit": "kg", "reg": 0.0,
            "eta": (3, 8), "track": "有", "limit": 70.0, "note": "北美线优势"},
    "fedex": {"name": "FedEx 国际快递", "mode": "weight",
              "first": 200.0, "add": 92.0, "unit": "kg", "reg": 0.0,
              "eta": (3, 8), "track": "有", "limit": 70.0, "note": "欧美线稳定"},
    "air_duty": {"name": "空派双清包税", "mode": "weight", "min_kg": 12.0,
                 "first": 0.0, "add": 42.0, "unit": "kg", "reg": 0.0,
                 "eta": (8, 14), "track": "有", "limit": 1000.0,
                 "note": "含关税，重货批量补货主力"},
    "sea_us": {"name": "海派美森快船(美国)", "mode": "weight", "min_kg": 21.0,
               "first": 0.0, "add": 13.5, "unit": "kg", "reg": 0.0,
               "eta": (25, 40), "track": "有", "limit": 100000.0,
               "note": "最便宜，旺季必配，需提前 45 天备货"},
    "sea_eu": {"name": "海派欧洲专线", "mode": "weight", "min_kg": 21.0,
               "first": 0.0, "add": 15.0, "unit": "kg", "reg": 0.0,
               "eta": (30, 45), "track": "有", "limit": 100000.0,
               "note": "欧洲海运双清，时效慢于美线"},
}

# ============================================================================
# 三、平台费率
# ============================================================================
PLATFORM_FEE = {
    "amazon": {"name": "Amazon US", "referral": 0.15, "fixed": 0.30,
               "fba": [(0.1, 3.22), (0.25, 3.86), (0.5, 4.75), (1.0, 6.05),
                       (2.0, 8.26), (5.0, 11.5)],
               "note": "佣金按类目 8%~15%，此处取 15% 电子类常见值"},
    "ebay": {"name": "eBay US", "referral": 0.129, "fixed": 0.30, "fba": None,
             "note": "最终价值费 ~12.9% + 每单 0.3 USD"},
    "shopify": {"name": "Shopify(Shopify Payments)", "referral": 0.029, "fixed": 0.30,
                "fba": None, "note": "2.9% + 0.3 USD，自建站无平台佣金"},
    "aliexpress": {"name": "AliExpress", "referral": 0.08, "fixed": 0.0, "fba": None,
                   "note": "佣金约 5%~8%"},
    "temu": {"name": "Temu 全托管", "referral": 0.0, "fixed": 0.0, "fba": None,
             "note": "平台定价，卖家只报供货价，无佣金但毛利被压"},
}

# HS 编码示例库（报关要素按 HS 生成）
HS_DB = {
    "8518.30": {"name": "耳机及听筒（含蓝牙耳机）", "duty_us": 0.0, "duty_eu": 0.046,
                "elements": ["品名", "用途（蓝牙耳机/有线）", "品牌", "型号",
                             "是否无线", "连接方式（蓝牙版本）", "材质"]},
    "8517.62": {"name": "无线通信设备（手机/路由器）", "duty_us": 0.0, "duty_eu": 0.0,
                "elements": ["品名", "用途", "品牌", "型号", "通信方式", "是否带屏幕"]},
    "3926.90": {"name": "塑料制品（手机壳/日用件）", "duty_us": 0.053, "duty_eu": 0.065,
                "elements": ["品名", "材质（PC/TPU/硅胶）", "用途", "品牌", "型号"]},
    "6109.10": {"name": "针织 T 恤", "duty_us": 0.165, "duty_eu": 0.12,
                "elements": ["品名", "织造方法（针织）", "成分含量", "品牌", "尺码", "性别"]},
    "9503.00": {"name": "玩具", "duty_us": 0.0, "duty_eu": 0.0,
                "elements": ["品名", "材质", "用途", "品牌", "适用年龄", "是否带电"]},
    "8513.10": {"name": "手电筒/便携灯具", "duty_us": 0.035, "duty_eu": 0.047,
                "elements": ["品名", "材质", "用途", "品牌", "型号", "电源类型"]},
}


def fba_fee(platform, weight_kg):
    p = PLATFORM_FEE.get(platform) or {}
    if not p.get("fba"):
        return 0.0
    for lim, fee in p["fba"]:
        if weight_kg <= lim:
            return fee
    return p["fba"][-1][1] + (weight_kg - p["fba"][-1][0]) * 1.2


def freight(channel, weight_kg, cbm=0.0):
    c = CHANNELS.get(channel)
    if not c:
        return {"ok": False, "error": "未知渠道 %s" % channel}
    # 海运/空派按最低起运量计费（21kg / 12kg），否则 0.2kg 走海派会算出 ¥2 的荒谬价
    w = max(weight_kg, 0.01, float(c.get("min_kg", 0)))
    if c["mode"] == "weight":
        if c["first"] == 0.0:          # 纯续重计费（空派/海派）
            cost = c["add"] * w
        else:
            cost = c["first"] + c["add"] * max(0.0, w - 0.5) + c["reg"]
    else:
        cost = c["add"] * w
    if weight_kg > c["limit"]:
        return {"ok": False, "error": "超重（渠道上限 %s kg）" % c["limit"]}
    return {"ok": True, "channel": channel, "name": c["name"], "cost_cny": round(cost, 2),
            "eta": c["eta"], "note": c["note"], "track": c["track"]}


def calc_profit(cost_cny, weight_kg, price_usd, platform="amazon", channel="yunexpress",
                ad_rate=0.10, hs=None, fx=None, country="US", units=1,
                extra_pack_cny=3.0, return_rate=0.03):
    fx = fx or get_fx()
    usdcny = fx.get("USDCNY", 7.15)
    p = PLATFORM_FEE.get(platform) or PLATFORM_FEE["amazon"]
    fr = freight(channel, weight_kg)
    if not fr.get("ok"):
        return {"ok": False, "error": fr.get("error")}

    purchase_usd = cost_cny / usdcny
    freight_usd = fr["cost_cny"] / usdcny
    pack_usd = extra_pack_cny / usdcny
    # 关税按 CIF（货值+运费）计
    hs_info = HS_DB.get(hs) if hs else None
    duty_rate = 0.0
    if hs_info:
        duty_rate = hs_info.get("duty_us" if country.upper() == "US" else "duty_eu", 0.0)
    cif_usd = purchase_usd + freight_usd
    duty_usd = cif_usd * duty_rate

    referral_usd = price_usd * p["referral"]
    fixed_usd = p["fixed"]
    fba_usd = fba_fee(platform, weight_kg)
    ad_usd = price_usd * ad_rate
    return_usd = price_usd * return_rate

    total_cost = (purchase_usd + freight_usd + pack_usd + duty_usd +
                  referral_usd + fixed_usd + fba_usd + ad_usd + return_usd)
    gross = price_usd - total_cost
    margin = gross / price_usd if price_usd else 0
    roi = gross / (purchase_usd + freight_usd + pack_usd) if (purchase_usd + freight_usd + pack_usd) else 0
    breakeven = total_cost / (1 - p["referral"] - ad_rate - return_rate)

    return {
        "ok": True, "currency": "USD", "fx_usdcny": usdcny, "fx_source": fx.get("_source"),
        "platform": platform, "platform_name": p["name"], "channel": channel,
        "channel_name": fr["name"], "eta_days": fr["eta"],
        "sell_price": round(price_usd, 2),
        "breakdown": {
            "采购成本": round(purchase_usd, 2),
            "头程运费": round(freight_usd, 2),
            "包装杂费": round(pack_usd, 2),
            "关税(%.1f%%)" % (duty_rate * 100): round(duty_usd, 2),
            "平台佣金(%.1f%%)" % (p["referral"] * 100): round(referral_usd, 2),
            "平台固定费": round(fixed_usd, 2),
            "FBA/履约费": round(fba_usd, 2),
            "广告费(%.0f%%)" % (ad_rate * 100): round(ad_usd, 2),
            "退货损耗(%.0f%%)" % (return_rate * 100): round(return_usd, 2),
        },
        "total_cost": round(total_cost, 2),
        "gross_profit": round(gross, 2),
        "gross_margin": round(margin * 100, 2),
        "roi": round(roi * 100, 1),
        "breakeven_price": round(breakeven, 2),
        "fob_cny": round(cost_cny + extra_pack_cny, 2),
        "cif_usd": round(cif_usd, 2),
        "monthly_profit_at_1000_units": round(gross * 1000, 2),
        "hs": hs, "hs_name": (hs_info or {}).get("name"),
        "customs_elements": (hs_info or {}).get("elements"),
    }


def calc_b2b(cost_cny, weight_kg, qty, channel="sea_us", margin_target=0.25,
             duty_rate=0.0, fx=None, pack_cny=3.0, country="US"):
    """B2B 批发报价核算 —— 与 B2C（calc_profit）完全分开：
    批发单没有平台佣金 / FBA / 广告费，运费按整批摊到每 pcs，
    报价 = (单位成本 + 关税) / (1 - 目标毛利率)。
    早期版本直接拿零售价模型给批发客户报价，结果算出 38.5 USD 去对客户 8.5 USD 的
    采购目标价，差 30 美元 —— 那是把两套生意混在一起了。"""
    fx = fx or get_fx()
    usdcny = fx.get("USDCNY", 7.15)
    fr = freight(channel, weight_kg * qty)
    if not fr.get("ok"):
        return {"ok": False, "error": fr.get("error")}
    unit_purchase = (cost_cny + pack_cny) / usdcny
    unit_freight = fr["cost_cny"] / usdcny / qty
    unit_cost = unit_purchase + unit_freight
    unit_duty = unit_cost * duty_rate
    price = (unit_cost + unit_duty) / (1 - margin_target)
    c = CHANNELS.get(channel, {})
    return {
        "ok": True, "currency": "USD", "fx_usdcny": usdcny, "fx_source": fx.get("_source"),
        "qty": qty, "channel": channel, "channel_name": c.get("name", channel),
        "eta": "%d-%d天" % tuple(c.get("eta", (0, 0))),
        "breakdown": {"采购成本/只": round(unit_purchase, 3),
                      "头程运费/只": round(unit_freight, 3),
                      "关税/只": round(unit_duty, 3)},
        "unit_cost": round(unit_cost + unit_duty, 3),
        "quote_price": round(price, 2),
        "gross_margin": round(margin_target * 100, 2),
        "order_total": round(price * qty, 2),
        "freight_total_cny": round(fr["cost_cny"], 2),
        "fob_unit": round((cost_cny + pack_cny) / usdcny, 3),
        "cif_unit": round(unit_cost, 3),
    }


def compare_channels(weight_kg, cost_cny, price_usd, platform="amazon",
                     ad_rate=0.10, hs=None, fx=None, country="US"):
    fx = fx or get_fx()
    rows = []
    for k in CHANNELS:
        r = calc_profit(cost_cny, weight_kg, price_usd, platform, k, ad_rate, hs, fx, country)
        if r.get("ok"):
            rows.append({"channel": k, "name": r["channel_name"],
                         "freight_cny": round(r["breakdown"]["头程运费"] * r["fx_usdcny"], 2),
                         "eta": "%d-%d天" % tuple(r["eta_days"]),
                         "gross": r["gross_profit"], "margin": r["gross_margin"],
                         "roi": r["roi"]})
    rows.sort(key=lambda x: -x["gross"])
    return rows


# ============================================================================
# 四、1688 / 阿里国际站 抓取
# ============================================================================
def http_get(url, timeout=25, headers=None):
    hdr = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        hdr.update(headers)
    try:
        if requests is not None:
            r = requests.get(url, headers=hdr, timeout=timeout,
                             proxies={"http": PROXY, "https": PROXY})
            return {"status": r.status_code, "text": r.text}
        import urllib.request
        req = urllib.request.Request(url, headers=hdr)
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        return {"status": 200, "text": op.open(req, timeout=timeout).read().decode("utf-8", "ignore")}
    except Exception as e:
        return {"status": getattr(e, "code", 0), "text": "", "err": str(e)[:80]}


def search_1688(kw, maxn=10, use_browser=True):
    url = "https://s.1688.com/selloffer/offer_search.htm?keywords=" + quote(kw)
    r = http_get(url, 25)
    if r["status"] == 200 and len(r["text"]) > 20000:
        titles = re.findall(r'"title":"([^"]{6,120})"', r["text"])
        prices = re.findall(r'"price"\s*:\s*"?([\d.]+)"?', r["text"])
        rows = []
        for i, t in enumerate(titles[:maxn]):
            rows.append({"source": "1688", "title": re.sub(r"<[^>]+>", "", t)[:120],
                         "price_cny": float(prices[i]) if i < len(prices) else None,
                         "url": url})
        if rows:
            return rows, "requests 直取成功"
    if not use_browser:
        return [], "requests 失败（1688 需登录态），未启用浏览器"
    try:
        from playwright.sync_api import sync_playwright
        out = []
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, proxy={"server": PROXY})
            ctx = b.new_context(user_agent=UA, locale="zh-CN")
            pg = ctx.new_page()
            pg.goto(url, timeout=60000, wait_until="domcontentloaded")
            pg.wait_for_timeout(4000)
            cards = pg.query_selector_all("[class*='offer'], .offer-item, .item")[:maxn * 3]
            seen = set()
            for c in cards[:maxn * 4]:
                t = c.inner_text().strip().replace("\n", " ")[:120]
                if len(t) < 8:
                    continue
                # 1688 的商品卡片是嵌套结构，同一个商品会被外层容器重复命中 5 次，
                # 不去重会把一条货源当五条，采购比价直接失真
                key = t[:40]
                if key in seen:
                    continue
                seen.add(key)
                m = re.search(r"[¥￥]\s*([\d.]+)", t)
                out.append({"source": "1688", "title": t,
                            "price_cny": float(m.group(1)) if m else None, "url": url})
                if len(out) >= maxn:
                    break
            b.close()
        return out, ("浏览器渲染取到 %d 条" % len(out)) if out else "浏览器渲染后仍无商品卡片（1688 登录墙）"
    except Exception as e:
        return [], "浏览器失败 %s: %s" % (type(e).__name__, str(e)[:60])


def search_alibaba(kw, maxn=10, use_browser=True):
    url = "https://www.alibaba.com/trade/search?SearchText=" + quote(kw)
    r = http_get(url, 25)
    rows = []
    if r["status"] == 200:
        titles = re.findall(r'"title":"([^"]{8,150})"', r["text"])
        prices = re.findall(r'"price"\s*:\s*"?([\d.]+)"?', r["text"])
        moq = re.findall(r'"moq"\s*:\s*"?(\d+)"?', r["text"])
        seen = set()
        for i, t in enumerate(titles):
            if t in seen:
                continue
            seen.add(t)
            rows.append({"source": "alibaba", "title": t[:140],
                         "price_usd": float(prices[i]) if i < len(prices) else None,
                         "moq": int(moq[i]) if i < len(moq) else None, "url": url})
            if len(rows) >= maxn:
                break
    if rows:
        return rows, "requests 直取成功"
    if not use_browser:
        return [], "requests 失败，未启用浏览器"
    try:
        from playwright.sync_api import sync_playwright
        out = []
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, proxy={"server": PROXY})
            ctx = b.new_context(user_agent=UA, locale="en-US")
            pg = ctx.new_page()
            pg.goto(url, timeout=60000, wait_until="domcontentloaded")
            pg.wait_for_timeout(4000)
            for c in pg.query_selector_all("[class*='item'], .organic-list-offer")[:maxn]:
                t = c.inner_text().strip().replace("\n", " ")[:140]
                if len(t) < 10:
                    continue
                m = re.search(r"US\$\s*([\d.,]+)", t)
                out.append({"source": "alibaba", "title": t,
                            "price_usd": float(m.group(1).replace(",", "")) if m else None,
                            "url": url})
            b.close()
        return out, ("浏览器渲染取到 %d 条" % len(out)) if out else "浏览器渲染后仍无可解析卡片"
    except Exception as e:
        return [], "浏览器失败 %s: %s" % (type(e).__name__, str(e)[:60])


# ============================================================================
# CLI
# ============================================================================
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("search")
    s.add_argument("kw"); s.add_argument("--source", default="alibaba,1688")
    s.add_argument("--max", type=int, default=10); s.add_argument("--out", default="sourcing.json")
    c = sub.add_parser("calc")
    c.add_argument("--cost", type=float, required=True, help="采购单价 CNY")
    c.add_argument("--weight", type=float, required=True, help="实重 kg")
    c.add_argument("--price", type=float, required=True, help="售价 USD")
    c.add_argument("--platform", default="amazon", choices=list(PLATFORM_FEE))
    c.add_argument("--channel", default="yunexpress", choices=list(CHANNELS))
    c.add_argument("--ad", type=float, default=0.10, help="广告费占售价比")
    c.add_argument("--hs", default=None); c.add_argument("--country", default="US")
    c.add_argument("--out", default=None)
    ch = sub.add_parser("channels")
    ch.add_argument("--weight", type=float, required=True)
    ch.add_argument("--cost", type=float, default=40)
    ch.add_argument("--price", type=float, default=29.99)
    ch.add_argument("--platform", default="amazon"); ch.add_argument("--country", default="US")
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help(); return

    if a.cmd == "search":
        allrows, notes = [], []
        for src in a.source.split(","):
            if src.strip() == "1688":
                rows, n = search_1688(a.kw, a.max)
            elif src.strip() == "alibaba":
                rows, n = search_alibaba(a.kw, a.max)
            else:
                continue
            notes.append("%s: %s" % (src, n))
            allrows.extend(rows)
        print("[*] 供应链检索 %r" % a.kw)
        for n in notes:
            print("    " + n)
        print("\n%-10s %-56s %12s" % ("来源", "标题", "价格"))
        print("-" * 84)
        for r in allrows:
            print("%-10s %-56s %12s" % (r["source"], r["title"][:56],
                                        r.get("price_cny") or r.get("price_usd") or "-"))
        json.dump({"keyword": a.kw, "notes": notes, "items": allrows},
                  open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("\n[+] 已写 %s（共 %d 条）" % (a.out, len(allrows)))

    elif a.cmd == "calc":
        r = calc_profit(a.cost, a.weight, a.price, a.platform, a.channel,
                        a.ad, a.hs, None, a.country)
        if not r.get("ok"):
            print("计算失败：%s" % r.get("error")); return
        print("\n利润测算（汇率 USD/CNY=%.4f，来源 %s）" % (r["fx_usdcny"], r["fx_source"]))
        print("  平台 %s ｜ 渠道 %s ｜ 时效 %s 天" % (
            r["platform_name"], r["channel_name"], "-".join(map(str, r["eta_days"]))))
        print("  售价 USD %.2f" % r["sell_price"])
        for k, v in r["breakdown"].items():
            print("    %-18s %8.2f" % (k, v))
        print("  " + "-" * 34)
        print("    总成本           %8.2f" % r["total_cost"])
        print("    毛利             %8.2f  （毛利率 %.2f%%，ROI %.1f%%）" % (
            r["gross_profit"], r["gross_margin"], r["roi"]))
        print("    盈亏平衡售价     %8.2f" % r["breakeven_price"])
        print("    FOB %.2f CNY ｜ CIF %.2f USD" % (r["fob_cny"], r["cif_usd"]))
        if r.get("customs_elements"):
            print("    报关要素：%s" % "、".join(r["customs_elements"]))
        if a.out:
            json.dump(r, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print("[+] 已写 %s" % a.out)

    elif a.cmd == "channels":
        rows = compare_channels(a.weight, a.cost, a.price, a.platform, 0.10, None, None, a.country)
        print("\n渠道比价（重 %.2f kg，售价 %.2f USD，平台 %s）" % (a.weight, a.price, a.platform))
        print("%-20s %10s %10s %10s %8s %8s" % ("渠道", "运费CNY", "时效", "毛利USD", "毛利率", "ROI"))
        print("-" * 74)
        for r in rows:
            print("%-20s %10.2f %10s %10.2f %7.2f%% %7.1f%%" % (
                r["name"], r["freight_cny"], r["eta"], r["gross"], r["margin"], r["roi"]))


if __name__ == "__main__":
    main()
