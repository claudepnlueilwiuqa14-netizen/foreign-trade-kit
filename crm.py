#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
crm.py —— 外贸询盘 CRM：跟进 / 客户分层 / 报价单生成（HTML，可直接打印存 PDF）

数据落盘：crm_data.json（纯 JSON，无需数据库）
客户分层：A/B/C/D 四类，按「意向分 × 预估金额 × 询盘次数」加权，并叠加最近联系天数衰减

用法：
  python crm.py add --name "John Smith" --country US --email john@x.com \
         --product "无线蓝牙耳机" --qty 500 --target 6.5 --source alibaba --intent 8
  python crm.py list --tier A
  python crm.py follow --id 1 --note "发了报价单，等待回复" --next 3
  python crm.py quote --id 1 --cost 42 --weight 0.18 --channel yunexpress --platform amazon
  python crm.py funnel           # 漏斗与分层统计
"""
import os
import io
import sys
import json
import time
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sourcing import calc_profit, calc_b2b, freight, CHANNELS, get_fx  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crm_data.json")
STAGES = ["新询盘", "已报价", "谈判中", "已打样", "已下单", "已流失"]
TIER_RULE = {"A": "高意向 + 高金额，重点跟进（48h 内必回）",
             "B": "有意向，金额中等，持续培育",
             "C": "价格敏感/小额，走标准化模板批量处理",
             "D": "长期沉默或已流失，季度唤醒一次"}


def load():
    if os.path.exists(DB):
        return json.load(open(DB, encoding="utf-8"))
    return {"leads": [], "seq": 0}


def save(d):
    json.dump(d, open(DB, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def tier_of(lead):
    score = lead.get("intent", 5)                       # 1-10 人工意向分
    amount = min((lead.get("qty") or 0) * (lead.get("target_price") or 0), 200000)
    amt_s = min(amount / 20000.0, 5.0)                  # 金额分 0-5
    cnt_s = min(len(lead.get("follows") or []), 3.0)    # 跟进次数 0-3
    days = 999
    if lead.get("last_contact"):
        d0 = datetime.datetime.strptime(lead["last_contact"][:10], "%Y-%m-%d").date()
        days = (datetime.date.today() - d0).days
    decay = 1.0 if days <= 7 else (0.8 if days <= 21 else (0.6 if days <= 45 else 0.3))
    total = (score + amt_s + cnt_s) * decay
    if lead.get("stage") == "已流失":
        return "D", total
    if total >= 13:
        return "A", total
    if total >= 9:
        return "B", total
    if total >= 5:
        return "C", total
    return "D", total


def add_lead(name, country, email, product, qty, target_price, source, intent, company="", note=""):
    d = load()
    d["seq"] += 1
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lead = {"id": d["seq"], "name": name, "company": company, "country": country.upper(),
            "email": email, "product": product, "qty": qty, "target_price": target_price,
            "source": source, "intent": intent, "stage": "新询盘",
            "created": now, "last_contact": now, "next_follow": None,
            "follows": [{"t": now, "note": note or "首次询盘"}]}
    lead["tier"], lead["score"] = tier_of(lead)
    d["leads"].append(lead)
    save(d)
    return lead


def follow(id_, note, next_days=None):
    d = load()
    for l in d["leads"]:
        if l["id"] == id_:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            l["follows"].append({"t": now, "note": note})
            l["last_contact"] = now
            if next_days:
                l["next_follow"] = (datetime.date.today() +
                                    datetime.timedelta(days=next_days)).isoformat()
            l["tier"], l["score"] = tier_of(l)
            save(d)
            return l
    return None


def set_stage(id_, stage):
    d = load()
    for l in d["leads"]:
        if l["id"] == id_:
            l["stage"] = stage
            l["tier"], l["score"] = tier_of(l)
            save(d)
            return l
    return None


def funnel(d=None):
    d = d or load()
    by_stage = {s: 0 for s in STAGES}
    by_tier = {"A": 0, "B": 0, "C": 0, "D": 0}
    amt = 0.0
    for l in d["leads"]:
        by_stage[l.get("stage", "新询盘")] = by_stage.get(l.get("stage", "新询盘"), 0) + 1
        by_tier[l.get("tier", "D")] = by_tier.get(l.get("tier", "D"), 0) + 1
        amt += (l.get("qty") or 0) * (l.get("target_price") or 0)
    today = datetime.date.today().isoformat()
    due = [l for l in d["leads"] if l.get("next_follow") and l["next_follow"] <= today]
    return {"total": len(d["leads"]), "by_stage": by_stage, "by_tier": by_tier,
            "pipeline_usd": round(amt, 2), "follow_due_today": [
                {"id": l["id"], "name": l["name"], "tier": l["tier"], "next": l["next_follow"]}
                for l in due]}


# ---------------------------------------------------------------- 报价单
def quote_html(lead, cost_cny, weight_kg, channel="yunexpress", platform="amazon",
               margin_target=0.25, fx=None, out=None, validity_days=15,
               payment="T/T 30% 定金，发货前付清余款", incoterm="FOB Shenzhen"):
    fx = fx or get_fx()
    target_price = lead.get("target_price")
    # 批发报价走 B2B 模型（无平台佣金/FBA/广告，运费按整批摊）
    qty = lead.get("qty") or 1
    r = calc_b2b(cost_cny, weight_kg, qty, channel, margin_target, 0.0, fx)
    if not r.get("ok"):
        raise RuntimeError(r.get("error"))
    price = r["quote_price"]
    # 若客户目标价低于成本价，给出"最接近可接单价"（毛利率压到 8%）
    floor = None
    if target_price and target_price < price:
        rf = calc_b2b(cost_cny, weight_kg, qty, channel, 0.08, 0.0, fx)
        floor = {"price": rf["quote_price"], "margin": 8.0,
                 "note": "目标价 %.2f 低于 %.0f%% 毛利对应的 %.2f。可接底线 %.2f（8%% 毛利），"
                         "差额需靠提 MOQ、降规格或改海运渠道消化。"
                         % (target_price, margin_target * 100, price, rf["quote_price"])}
    r["floor"] = floor
    r["gross_margin"] = r["gross_margin"]
    r["roi"] = round((price - r["unit_cost"]) / r["unit_cost"] * 100, 1)
    eta = CHANNELS.get(channel, {}).get("eta", (0, 0))
    rows = "".join("<tr><td>%s</td><td class='n'>%.3f</td></tr>" % (k, v)
                   for k, v in r["breakdown"].items())
    rows += ("<tr><td><b>单位成本合计</b></td><td class='n'>%.3f</td></tr>"
             "<tr><td>FOB 单价 / CIF 单价</td><td class='n'>%.3f / %.3f</td></tr>"
             % (r["unit_cost"], r["fob_unit"], r["cif_unit"]))
    expires = (datetime.date.today() + datetime.timedelta(days=validity_days)).isoformat()
    ok = target_price is None or price <= target_price
    html = """<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">
<title>报价单 %(name)s</title><style>
body{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:880px;margin:24px auto;color:#1c2330}
.hd{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:3px solid #2b6cb0;padding-bottom:10px}
h1{margin:0;font-size:22px}
table{border-collapse:collapse;width:100%%;margin:14px 0}
td,th{border:1px solid #c8d0da;padding:7px 10px;font-size:14px}
th{background:#eef3f9}
.n{text-align:right;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 24px;font-size:14px;margin:12px 0}
.big{font-size:26px;color:#c53030;font-weight:700}
.warn{background:#fffafc;border-left:4px solid #c53030;padding:8px 12px;font-size:13px;margin:12px 0}
.ok{background:#f0fff4;border-left:4px solid #2f855a;padding:8px 12px;font-size:13px;margin:12px 0}
.ft{color:#8a94a6;font-size:12px;margin-top:18px;border-top:1px dashed #cbd5e0;padding-top:8px}
@media print{button{display:none}}
</style>
<div class="hd"><div><h1>QUOTATION 报价单</h1><div style="color:#6b7280">No. Q%(id)s-%(ts)s</div></div>
<div style="text-align:right;color:#6b7280">有效期至 %(expires)s</div></div>
<div class="grid">
<div>客户：%(name)s %(company)s</div><div>国家：%(country)s</div>
<div>邮箱：%(email)s</div><div>来源：%(source)s ｜ 分层 %(tier)s 类</div>
<div>产品：%(product)s</div><div>数量：%(qty)s pcs</div>
</div>
<h3>报价</h3>
<table><tr><th>项目</th><th class='n'>USD</th></tr>%(rows)s
<tr><td><b>单价（建议报价）</b></td><td class='n big'>%(price)s</td></tr>
<tr><td>总价（%(qty)s pcs）</td><td class='n'><b>%(total).2f USD</b></td></tr>
<tr><td>毛利率 / ROI</td><td class='n'>%(margin).2f%% / %(roi).1f%%</td></tr>
</table>
%(flag)s
<h3>条款</h3>
<div class="grid">
<div>贸易术语：%(incoterm)s</div><div>付款方式：%(payment)s</div>
<div>物流渠道：%(chname)s（%(eta)d-%(eta2)d 天）</div><div>汇率：1 USD = %(fx).4f CNY（%(fxsrc)s）</div>
<div>打样周期：7-10 天</div><div>量产交期：25-35 天（视数量）</div>
</div>
<div class="ft">本报价单由 tradekit/crm.py 自动生成。运费与平台费率取自内置渠道表，正式合同以货代实时报价与平台政策为准。
生成时间：%(now)s</div></html>""" % {
        "id": lead["id"], "name": lead["name"], "company": lead.get("company", ""),
        "country": lead["country"], "email": lead.get("email", ""),
        "source": lead.get("source", ""), "tier": lead.get("tier", ""),
        "product": lead.get("product", ""), "qty": lead.get("qty", 0),
        "rows": rows, "price": price, "total": price * (lead.get("qty") or 0),
        "margin": r["gross_margin"], "roi": r["roi"], "incoterm": incoterm,
        "payment": payment, "chname": CHANNELS.get(channel, {}).get("name", channel),
        "eta": eta[0], "eta2": eta[1], "fx": fx.get("USDCNY", 7.15),
        "fxsrc": fx.get("_source", "fallback"), "expires": expires,
        "ts": time.strftime("%Y%m%d"), "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "flag": (("<div class='ok'>客户目标价 %.2f USD —— 报价 %.2f USD 在其预算内，可直接推进。</div>"
                  % (target_price, price)) if (target_price and price <= target_price) else
                 ("<div class='warn'>%s</div>" % floor["note"]) if floor else
                 ("<div class='ok'>未登记目标价，按目标毛利 %.0f%% 报出。</div>"
                  % (margin_target * 100))),
    }
    out = out or "报价单_%d.html" % lead["id"]
    open(out, "w", encoding="utf-8").write(html)
    r.update({"quote_price": price, "html": out, "expires": expires,
              "customer_target": target_price,
              "gap": (round(price - target_price, 2) if target_price else None)})
    return r


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("--name", required=True); a.add_argument("--country", default="US")
    a.add_argument("--email", default=""); a.add_argument("--company", default="")
    a.add_argument("--product", required=True); a.add_argument("--qty", type=int, default=100)
    a.add_argument("--target", type=float, default=0)
    a.add_argument("--source", default="alibaba"); a.add_argument("--intent", type=int, default=5)
    a.add_argument("--note", default="")
    l = sub.add_parser("list"); l.add_argument("--tier"); l.add_argument("--stage")
    f = sub.add_parser("follow")
    f.add_argument("--id", type=int, required=True); f.add_argument("--note", required=True)
    f.add_argument("--next", type=int, default=None)
    s = sub.add_parser("stage")
    s.add_argument("--id", type=int, required=True); s.add_argument("--to", required=True, choices=STAGES)
    q = sub.add_parser("quote")
    q.add_argument("--id", type=int, required=True); q.add_argument("--cost", type=float, required=True)
    q.add_argument("--weight", type=float, required=True); q.add_argument("--channel", default="yunexpress")
    q.add_argument("--platform", default="amazon"); q.add_argument("--margin", type=float, default=0.25)
    q.add_argument("--out", default=None)
    sub.add_parser("funnel")
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help(); return

    if args.cmd == "add":
        lead = add_lead(args.name, args.country, args.email, args.product, args.qty,
                        args.target, args.source, args.intent, args.company, args.note)
        print("[+] 新增询盘 #%d %s（%s 类客户，得分 %.1f）" % (
            lead["id"], lead["name"], lead["tier"], lead["score"]))

    elif args.cmd == "list":
        d = load()
        rows = d["leads"]
        if args.tier:
            rows = [r for r in rows if r.get("tier") == args.tier.upper()]
        if args.stage:
            rows = [r for r in rows if r.get("stage") == args.stage]
        print("%-4s %-18s %-6s %-4s %-10s %8s %9s %8s" % (
            "ID", "客户", "国家", "层", "阶段", "数量", "目标价", "得分"))
        print("-" * 80)
        for r in rows:
            print("%-4d %-18s %-6s %-4s %-10s %8s %9s %8.1f" % (
                r["id"], r["name"][:18], r["country"], r.get("tier"), r["stage"],
                r.get("qty"), r.get("target_price"), r.get("score", 0)))
        print("-" * 80)
        print("共 %d 条（A=%s）" % (len(rows), funnel(d)["by_tier"]))

    elif args.cmd == "follow":
        r = follow(args.id, args.note, args.next)
        print("[+] 已记录跟进 #%d，当前 %s 类，下次跟进 %s" % (
            args.id, r["tier"], r.get("next_follow") or "未设置") if r else "[!] 未找到该 ID")

    elif args.cmd == "stage":
        r = set_stage(args.id, args.to)
        print("[+] #%d 阶段 -> %s（%s 类）" % (args.id, args.to, r["tier"]) if r else "[!] 未找到")

    elif args.cmd == "quote":
        d = load()
        lead = next((x for x in d["leads"] if x["id"] == args.id), None)
        if not lead:
            print("[!] 未找到询盘 #%d" % args.id); return
        r = quote_html(lead, args.cost, args.weight, args.channel, args.platform,
                       args.margin, None, args.out)
        print("[+] 报价单：%s ｜ 建议报价 %.2f USD ｜ 毛利率 %.2f%% ｜ ROI %.1f%%" % (
            r["html"], r["quote_price"], r["gross_margin"], r["roi"]))
        if r.get("gap"):
            print("    与客户目标价差额 %+.2f USD" % r["gap"])

    elif args.cmd == "funnel":
        f = funnel()
        print("\n客户总数 %d ｜ 线索总金额 %.2f USD" % (f["total"], f["pipeline_usd"]))
        print("阶段分布：%s" % f["by_stage"])
        print("分层分布：A=%d B=%d C=%d D=%d" % tuple(f["by_tier"][k] for k in "ABCD"))
        print("今日待跟进：%s" % (f["follow_due_today"] or "无"))
        for k, v in TIER_RULE.items():
            print("  %s 类：%s" % (k, v))


if __name__ == "__main__":
    main()
