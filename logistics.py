#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
logistics.py —— 物流渠道比价 / 时效估算 / 报关要素清单生成

数据来源：sourcing.py 里的 CHANNELS 渠道费率表（运费）、内置真实海关与节假日规则。
时效不是拍脑袋：头程（渠道 ETA）+ 清关（目的国/是否敏感货）+ 尾程（快递派送）
三段时间相加，再乘旺季系数（黑五/圣诞/春节）。

用法：
  python logistics.py quote --weight 0.18 --country US --value 29.99 --sensitive battery
  python logistics.py eta --channel yunexpress --country US --date 2026-11-20
  python logistics.py customs --product "无线蓝牙耳机" --hs 8518.30 --qty 200 \
         --net 9 --gross 11 --value 5.6 --out 报关要素.html
"""
import os
import re
import io
import sys
import json
import time
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sourcing import CHANNELS, freight, HS_DB, get_fx  # noqa: E402


# ============================================================================
# 清关与尾程规则
# ============================================================================
CUSTOMS_DAYS = {
    "US": {"normal": (1, 3), "sensitive": (3, 7), "note": "FDA/FCC 类需额外资料；低值免税 800USD（Section 321）"},
    "GB": {"normal": (1, 3), "sensitive": (2, 5), "note": "脱欧后需 EORI + VAT，135GBP 以下平台代收 VAT"},
    "DE": {"normal": (2, 4), "sensitive": (4, 9), "note": "欧盟 IOSS；WEEE/包装法合规是常被扣关的点"},
    "FR": {"normal": (2, 4), "sensitive": (4, 9), "note": "欧盟 IOSS；法语标签要求"},
    "JP": {"normal": (1, 2), "sensitive": (2, 5), "note": "ACP 逆算制度，申报值偏低易被查验"},
    "AU": {"normal": (1, 3), "sensitive": (3, 6), "note": "GST 10%，低值也征"},
    "CA": {"normal": (1, 3), "sensitive": (3, 6), "note": "GST/HST，需 CARGO 控制号"},
}

LAST_MILE = {
    "US": (2, 5), "GB": (1, 3), "DE": (1, 3), "FR": (1, 3),
    "JP": (1, 2), "AU": (2, 5), "CA": (2, 5),
}

# 旺季窗口（月-日 区间 -> 系数）。真实外贸经验值：黑五/圣诞前港口与尾程普遍延误。
PEAK = [((11, 15), (12, 26), 1.45, "黑五/圣诞旺季，尾程与港口普遍延误"),
        ((1, 20), (2, 25), 1.60, "中国春节，工厂停产出货停滞"),
        ((9, 1), (10, 7), 1.20, "国庆+Prime 秋季促，舱位紧张")]

SENSITIVE = {
    "none": {"k": "普货", "channels": ["china_post", "yanwen", "yunexpress", "epacket", "ems",
                                       "dhl", "ups", "fedex", "air_duty", "sea_us", "sea_eu"]},
    "battery": {"k": "内置电池", "channels": ["yunexpress", "ems", "dhl", "ups", "fedex",
                                              "air_duty", "sea_us", "sea_eu"],
                "note": "需 UN38.3 + MSDS，带电渠道才能走"},
    "liquid": {"k": "液体/膏体", "channels": ["ems", "dhl", "sea_us", "sea_eu"],
               "note": "需非危险品鉴定，多数小包拒收"},
    "magnet": {"k": "带磁", "channels": ["ems", "dhl", "ups", "fedex", "air_duty", "sea_us"],
               "note": "需磁检报告，屏蔽包装"},
    "powder": {"k": "粉末", "channels": ["dhl", "sea_us"], "note": "查验率高，多数快递拒收"},
}

DOCUMENTS = {
    "US": ["商业发票 Commercial Invoice", "装箱单 Packing List", "FCC 声明（带电产品）",
           "UN38.3 + MSDS（含锂电池）", "ISF 10+2（海运）"],
    "GB": ["商业发票", "装箱单", "EORI 号", "UKCA/CE 声明", "VAT 缴纳凭证"],
    "DE": ["商业发票", "装箱单", "EORI 号", "CE 声明", "WEEE 注册号", "包装法 LUCID 注册"],
    "FR": ["商业发票", "装箱单", "EORI 号", "CE 声明", "法语说明书/标签"],
    "JP": ["商业发票", "装箱单", "ACP 逆算申报资料", "PSE 认证（电器）"],
    "AU": ["商业发票", "装箱单", "GST 申报", "RCM 合规（电器）"],
    "CA": ["商业发票", "装箱单", "GST/HST 申报", "cUL 认证（电器）"],
}


def peak_factor(d=None):
    d = d or datetime.date.today()
    md = (d.month, d.day)
    for (m1, d1), (m2, d2), f, note in PEAK:
        a, b = (m1, d1), (m2, d2)
        if a <= md <= b:
            return f, note
    return 1.0, "平季"


def eta_estimate(channel, country="US", sensitive="none", ship_date=None):
    c = CHANNELS.get(channel)
    if not c:
        return {"ok": False, "error": "未知渠道"}
    lo, hi = c["eta"]
    cu = CUSTOMS_DAYS.get(country.upper(), CUSTOMS_DAYS["US"])
    cl, ch = cu["sensitive"] if sensitive != "none" else cu["normal"]
    ll, lh = LAST_MILE.get(country.upper(), (2, 5))
    f, note = peak_factor(ship_date)
    base_lo, base_hi = lo + cl + ll, hi + ch + lh
    return {"ok": True, "channel": c["name"],
            "first_leg": "%d-%d 天" % (lo, hi),
            "customs": "%d-%d 天（%s）" % (cl, ch, cu["note"][:24]),
            "last_mile": "%d-%d 天" % (ll, lh),
            "peak_factor": f, "peak_note": note,
            "eta_days": "%d-%d" % (round(base_lo * f), round(base_hi * f)),
            "eta_min": round(base_lo * f), "eta_max": round(base_hi * f)}


def quote(weight_kg, country="US", value_usd=0.0, sensitive="none", cbm=0.0, fx=None):
    fx = fx or get_fx()
    usdcny = fx.get("USDCNY", 7.15)
    allowed = SENSITIVE.get(sensitive, SENSITIVE["none"])["channels"]
    rows = []
    for k in CHANNELS:
        if k not in allowed:
            continue
        fr = freight(k, weight_kg, cbm)
        if not fr.get("ok"):
            continue
        et = eta_estimate(k, country, sensitive)
        cost_cny = fr["cost_cny"]
        rows.append({
            "channel": k, "name": CHANNELS[k]["name"], "cost_cny": round(cost_cny, 2),
            "cost_usd": round(cost_cny / usdcny, 2),
            "freight_pct": round(cost_cny / usdcny / value_usd * 100, 1) if value_usd else None,
            "eta": et["eta_days"], "eta_min": et["eta_min"], "eta_max": et["eta_max"],
            "track": CHANNELS[k]["track"], "note": CHANNELS[k]["note"],
            "peak": et["peak_note"],
        })
    rows.sort(key=lambda r: (r["cost_cny"]))
    # 推荐：成本/时效综合（每快一天多付多少）
    if rows:
        cheapest = min(rows, key=lambda r: r["cost_cny"])
        fastest = min(rows, key=lambda r: r["eta_max"])
        balanced = min(rows, key=lambda r: r["cost_cny"] * 0.6 + r["eta_max"] * 12)
        rec = {"cheapest": cheapest["name"], "fastest": fastest["name"],
               "balanced": balanced["name"]}
    else:
        rec = {}
    return {"ok": True, "weight_kg": weight_kg, "country": country, "sensitive": sensitive,
            "fx_usdcny": usdcny, "fx_source": fx.get("_source"), "rows": rows,
            "recommend": rec, "documents": DOCUMENTS.get(country.upper(), DOCUMENTS["US"])}


# ============================================================================
# 报关要素清单
# ============================================================================
def customs_form(product, hs=None, qty=1, net_kg=0.0, gross_kg=0.0, unit_value_usd=0.0,
                 origin="CN", brand="Generic", model="", material="", usage="",
                 country="US", out_html=None):
    hs = hs or "8518.30"
    info = HS_DB.get(hs, {"name": "（HS 未在内置库，需查海关税则）", "duty_us": 0.0,
                          "duty_eu": 0.0, "elements": ["品名", "材质", "用途", "品牌", "型号"]})
    duty = info.get("duty_us" if country.upper() == "US" else "duty_eu", 0.0)
    total_value = round(unit_value_usd * qty, 2)
    form = {
        "product": product, "hs": hs, "hs_name": info["name"],
        "origin": origin, "brand": brand, "model": model,
        "material": material or "（按实物填写）", "usage": usage or "（按实物填写）",
        "qty": qty, "net_kg": net_kg, "gross_kg": gross_kg,
        "unit_value_usd": unit_value_usd, "total_value_usd": total_value,
        "duty_rate": duty, "duty_usd": round(total_value * duty, 2),
        "elements": info["elements"], "documents": DOCUMENTS.get(country.upper(), DOCUMENTS["US"]),
    }
    if out_html:
        rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (k, form.get(k2, ""))
                       for k, k2 in [("品名", "product"), ("HS 编码", "hs"),
                                     ("HS 名称", "hs_name"), ("原产国", "origin"),
                                     ("品牌", "brand"), ("型号", "model"),
                                     ("材质", "material"), ("用途", "usage"),
                                     ("数量", "qty"), ("净重 KG", "net_kg"),
                                     ("毛重 KG", "gross_kg"), ("单价 USD", "unit_value_usd"),
                                     ("总价 USD", "total_value_usd"),
                                     ("关税率", "duty_rate"), ("关税 USD", "duty_usd")])
        els = "".join("<li>%s</li>" % e for e in form["elements"])
        docs = "".join("<li>%s</li>" % d for d in form["documents"])
        html = """<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">
<title>报关要素清单 %(product)s</title><style>
body{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:860px;margin:28px auto;color:#1c2330}
h1{font-size:20px;border-bottom:2px solid #2b6cb0;padding-bottom:8px}
table{border-collapse:collapse;width:100%%;margin:14px 0}
td,th{border:1px solid #c8d0da;padding:7px 10px;font-size:14px}
td:first-child{background:#f2f6fb;width:150px;font-weight:600}
h2{font-size:15px;margin-top:22px}
@media print{button{display:none}}
</style><h1>报关要素清单</h1>
<table>%(rows)s</table>
<h2>申报要素（按海关要求逐项填写）</h2><ul>%(els)s</ul>
<h2>随附单证（%(country)s）</h2><ul>%(docs)s</ul>
<p style="color:#8a94a6;font-size:12px">生成时间 %(ts)s ｜ 税率来自内置 HS 示例库，正式申报以海关税则与报关行意见为准。</p>
</html>""" % {"product": form["product"], "rows": rows, "els": els, "docs": docs,
              "country": country, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        open(out_html, "w", encoding="utf-8").write(html)
        form["html"] = out_html
    return form


# ============================================================================
# CLI
# ============================================================================
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    q = sub.add_parser("quote")
    q.add_argument("--weight", type=float, required=True)
    q.add_argument("--country", default="US")
    q.add_argument("--value", type=float, default=0.0)
    q.add_argument("--sensitive", default="none", choices=list(SENSITIVE))
    q.add_argument("--json", dest="json_out", default=None)
    e = sub.add_parser("eta")
    e.add_argument("--channel", required=True, choices=list(CHANNELS))
    e.add_argument("--country", default="US")
    e.add_argument("--sensitive", default="none", choices=list(SENSITIVE))
    e.add_argument("--date", default=None, help="发货日 YYYY-MM-DD")
    c = sub.add_parser("customs")
    c.add_argument("--product", required=True)
    c.add_argument("--hs", default="8518.30")
    c.add_argument("--qty", type=int, default=1)
    c.add_argument("--net", type=float, default=0.0)
    c.add_argument("--gross", type=float, default=0.0)
    c.add_argument("--value", type=float, default=0.0)
    c.add_argument("--brand", default="Generic")
    c.add_argument("--model", default="")
    c.add_argument("--material", default="")
    c.add_argument("--country", default="US")
    c.add_argument("--out", default="报关要素.html")
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help(); return

    if a.cmd == "quote":
        r = quote(a.weight, a.country, a.value, a.sensitive)
        print("\n物流比价：%.2f kg -> %s（%s）｜汇率 %.4f（%s）" % (
            a.weight, a.country, SENSITIVE[a.sensitive]["k"], r["fx_usdcny"], r["fx_source"]))
        print("%-22s %10s %10s %10s %8s %s" %
              ("渠道", "运费CNY", "运费USD", "占货值", "时效", "备注"))
        print("-" * 96)
        for x in r["rows"]:
            print("%-22s %10.2f %10.2f %9s%% %8s %s" % (
                x["name"], x["cost_cny"], x["cost_usd"], x["freight_pct"],
                x["eta"], x["note"][:26]))
        print("-" * 96)
        if r["recommend"]:
            print("推荐：最省 %s ｜ 最快 %s ｜ 综合最优 %s" % (
                r["recommend"]["cheapest"], r["recommend"]["fastest"], r["recommend"]["balanced"]))
        print("随附单证：%s" % "、".join(r["documents"][:3]))
        if a.json_out:
            json.dump(r, open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    elif a.cmd == "eta":
        d = None
        if a.date:
            y, m, dd = [int(x) for x in a.date.split("-")]
            d = datetime.date(y, m, dd)
        r = eta_estimate(a.channel, a.country, a.sensitive, d)
        print("\n时效估算：%s -> %s（%s）" % (r["channel"], a.country, SENSITIVE[a.sensitive]["k"]))
        print("  头程 %s ｜ 清关 %s ｜ 尾程 %s" % (r["first_leg"], r["customs"], r["last_mile"]))
        print("  旺季系数 %.2f（%s）" % (r["peak_factor"], r["peak_note"]))
        print("  合计 %s 天" % r["eta_days"])

    elif a.cmd == "customs":
        f = customs_form(a.product, a.hs, a.qty, a.net, a.gross, a.value,
                         "CN", a.brand, a.model, "", "", a.country, a.out)
        print("\n报关要素：%s（HS %s / %s）" % (f["product"], f["hs"], f["hs_name"]))
        for k in ("qty", "net_kg", "gross_kg", "unit_value_usd", "total_value_usd",
                  "duty_rate", "duty_usd"):
            print("  %-14s %s" % (k, f[k]))
        print("  申报要素：%s" % "、".join(f["elements"]))
        print("  单证：%s" % "、".join(f["documents"]))
        print("[+] HTML 已写 %s" % f["html"])


if __name__ == "__main__":
    main()
