#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dashboard.py —— 外贸全流程本地看板（选品 → 利润 → 上架 → 订单 → 物流）

纯标准库 http.server，不装任何依赖就能跑。数据全部来自本目录真实的 *_data/demo_*.json，
缺哪个环节就在看板上显示"未导入"，**不编造**。

启动：
    python dashboard.py --port 8765
    curl http://127.0.0.1:8765/api/summary
    curl http://127.0.0.1:8765/api/pipeline
浏览器打开 http://127.0.0.1:8765/
"""
import os
import io
import re
import json
import time
import argparse
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# 数据文件 -> 环节
SOURCES = {
    "选品": ["demo_market.json", "market_data.json"],
    "利润": ["demo_profit.json"],
    "上架": ["listing.json"],
    "订单": ["crm_data.json"],
    "物流": ["demo_logistics.json"],
    "供应链": ["sourcing.json", "demo_sourcing.json"],
}


def _first_file(names):
    for n in names:
        p = os.path.join(HERE, n)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f), n, os.path.getmtime(p)
            except Exception:
                continue
    return None, None, None


def collect():
    out = {}
    # ---- 选品 ----
    d, fn, mt = _first_file(SOURCES["选品"])
    if d:
        items = d.get("items", [])
        prices = [i["price"] for i in items if i.get("price")]
        an = d.get("analysis", {})
        out["选品"] = {"file": fn, "keyword": d.get("keyword"),
                       "ts": datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M"),
                       "count": len(items),
                       "platforms": sorted({i.get("platform") for i in items}),
                       "price_median": an.get("price_median") or (
                           round(sorted(prices)[len(prices) // 2], 2) if prices else None),
                       "price_min": an.get("price_min") or (min(prices) if prices else None),
                       "price_max": an.get("price_max") or (max(prices) if prices else None),
                       "rating_mean": an.get("rating_mean"),
                       "top_keywords": [k for k, _ in (an.get("top_keywords") or [])[:12]],
                       "opportunity_count": len(an.get("opportunity", {}).get(
                           "low_rating_high_sold", [])),
                       "items": [{"platform": i.get("platform"), "title": (i.get("title") or "")[:70],
                                  "price": i.get("price"), "rating": i.get("rating"),
                                  "sold": i.get("sold_estimate")} for i in items[:20]]}
    else:
        out["选品"] = {"file": None, "count": 0, "note": "未导入：先跑 market.py search"}
    # ---- 利润 ----
    d, fn, mt = _first_file(SOURCES["利润"])
    if d and d.get("ok"):
        out["利润"] = {"file": fn, "platform": d.get("platform_name"),
                       "channel": d.get("channel_name"), "price": d.get("sell_price"),
                       "cost": d.get("total_cost"), "gross": d.get("gross_profit"),
                       "margin": d.get("gross_margin"), "roi": d.get("roi"),
                       "breakeven": d.get("breakeven_price"),
                       "breakdown": d.get("breakdown"), "fx": d.get("fx_usdcny")}
    else:
        out["利润"] = {"file": None, "note": "未导入：先跑 sourcing.py calc --out demo_profit.json"}
    # ---- 上架 ----
    d, fn, mt = _first_file(SOURCES["上架"])
    if d:
        out["上架"] = {"file": fn, "product": d.get("product"),
                       "platform": d.get("platform"),
                       "langs": [{"lang": l["lang_name"], "title": l["title"],
                                  "bullets": len(l.get("bullets") or [])}
                                 for l in d.get("listings", [])],
                       "kw_count": len((d.get("keywords") or {}).get("unigrams", []))}
    else:
        out["上架"] = {"file": None, "note": "未导入：先跑 listing.py gen --out listing.json"}
    # ---- 订单/询盘 ----
    d, fn, mt = _first_file(SOURCES["订单"])
    if d:
        leads = d.get("leads", [])
        stages = {}
        tiers = {"A": 0, "B": 0, "C": 0, "D": 0}
        amt = 0.0
        for l in leads:
            stages[l.get("stage", "新询盘")] = stages.get(l.get("stage", "新询盘"), 0) + 1
            tiers[l.get("tier", "D")] = tiers.get(l.get("tier", "D"), 0) + 1
            amt += (l.get("qty") or 0) * (l.get("target_price") or 0)
        today = datetime.date.today().isoformat()
        out["订单"] = {"file": fn, "count": len(leads), "stages": stages, "tiers": tiers,
                       "pipeline_usd": round(amt, 2),
                       "due": [{"id": l["id"], "name": l["name"], "tier": l["tier"]}
                               for l in leads
                               if l.get("next_follow") and l["next_follow"] <= today],
                       "leads": [{"id": l["id"], "name": l["name"], "country": l["country"],
                                  "tier": l.get("tier"), "stage": l.get("stage"),
                                  "qty": l.get("qty"), "target": l.get("target_price"),
                                  "score": round(l.get("score", 0), 1)} for l in leads]}
    else:
        out["订单"] = {"file": None, "count": 0, "note": "未导入：先跑 crm.py add"}
    # ---- 物流 ----
    d, fn, mt = _first_file(SOURCES["物流"])
    if d and d.get("rows"):
        out["物流"] = {"file": fn, "country": d.get("country"), "sensitive": d.get("sensitive"),
                       "recommend": d.get("recommend"),
                       "rows": [{"name": r["name"], "cost_cny": r["cost_cny"],
                                 "eta": r["eta"], "track": r["track"]}
                                for r in d["rows"][:8]]}
    else:
        out["物流"] = {"file": None, "note": "未导入：先跑 logistics.py quote --json demo_logistics.json"}
    # ---- 供应链 ----
    d, fn, mt = _first_file(SOURCES["供应链"])
    out["供应链"] = ({"file": fn, "count": len(d.get("items", [])),
                      "notes": d.get("notes", [])} if d else
                     {"file": None, "note": "未导入：先跑 sourcing.py search"})
    return out


def render_html(data):
    s = data["选品"]; p = data["利润"]; l = data["上架"]; o = data["订单"]; lg = data["物流"]
    def bar(items, key_name, key_val, unit="", color="#2b6cb0"):
        if not items:
            return '<div class="empty">未导入</div>'
        mx = max([i[key_val] or 0 for i in items] + [1])
        return "".join(
            '<div class="row"><span class="rn">%s</span>'
            '<span class="rb"><i style="width:%.1f%%;background:%s"></i></span>'
            '<span class="rv">%s%s</span></div>'
            % (i[key_name][:26], (i[key_val] or 0) / mx * 100, color, i[key_val], unit)
            for i in items)
    kw = "".join('<span class="chip">%s</span>' % k for k in (s.get("top_keywords") or [])[:12])
    items_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td class='n'>%s</td><td class='n'>%s</td><td class='n'>%s</td></tr>"
        % (i.get("platform", "")[:12], (i.get("title") or "")[:56], i.get("price") or "-",
           i.get("rating") or "-", i.get("sold") or "-") for i in (s.get("items") or [])[:14])
    lang_rows = "".join("<tr><td>%s</td><td>%s</td><td class='n'>%d</td></tr>"
                        % (x["lang"], x["title"][:70], x["bullets"])
                        for x in (l.get("langs") or []))
    lead_rows = "".join(
        "<tr><td class='n'>%d</td><td>%s</td><td>%s</td><td><b class='t%s'>%s</b></td>"
        "<td>%s</td><td class='n'>%s</td><td class='n'>%s</td><td class='n'>%.1f</td></tr>"
        % (x["id"], x["name"][:16], x["country"], x["tier"], x["tier"], x["stage"],
           x["qty"], x["target"], x["score"]) for x in (o.get("leads") or []))
    log_rows = "".join("<tr><td>%s</td><td class='n'>%.2f</td><td>%s</td><td>%s</td></tr>"
                       % (r["name"], r["cost_cny"], r["eta"], r["track"])
                       for r in (lg.get("rows") or []))
    profit_rows = "".join("<tr><td>%s</td><td class='n'>%.2f</td></tr>" % (k, v)
                          for k, v in (p.get("breakdown") or {}).items())
    rec = (lg.get("recommend") or {})
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>外贸全流程看板</title><style>
*{box-sizing:border-box}body{margin:0;background:#0d1117;color:#e6edf3;
font-family:-apple-system,"Microsoft YaHei",sans-serif;font-size:14px}
header{padding:16px 22px;border-bottom:1px solid #21262d;display:flex;gap:16px;align-items:baseline}
header h1{margin:0;font-size:18px}.sub{color:#7d8590;font-size:12px}
.wrap{padding:18px 22px;display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px 16px}
.card h2{margin:0 0 10px;font-size:14px;color:#58a6ff}
.kpi{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:10px}
.kpi div{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 12px;min-width:96px}
.kpi b{display:block;font-size:19px;color:#e6edf3}.kpi span{font-size:11px;color:#7d8590}
table{width:100%%;border-collapse:collapse;font-size:13px}
td,th{border-bottom:1px solid #21262d;padding:5px 6px;text-align:left}
th{color:#7d8590;font-weight:500}.n{text-align:right;font-variant-numeric:tabular-nums}
.row{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12px}
.rn{width:150px;color:#9aa4b2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rb{flex:1;background:#0d1117;border-radius:4px;height:12px;overflow:hidden}
.rb i{display:block;height:100%%}
.rv{width:64px;text-align:right;color:#e6edf3}
.chip{display:inline-block;background:#1f2937;border:1px solid #30363d;border-radius:20px;
padding:2px 10px;margin:3px 4px 0 0;font-size:12px;color:#a5d6ff}
.empty{color:#6e7681;font-size:12px;padding:8px 0}
.tA{color:#f85149}.tB{color:#d29922}.tC{color:#3fb950}.tD{color:#6e7681}
footer{padding:14px 22px;color:#6e7681;font-size:12px;border-top:1px solid #21262d}
a{color:#58a6ff}
</style></head><body>
<header><h1>外贸全流程看板</h1><span class="sub">选品 → 利润 → 上架 → 订单 → 物流 ｜ 数据源：本机 tradekit/*.json</span>
<span class="sub"><a href="/api/summary">/api/summary</a> · <a href="/api/pipeline">/api/pipeline</a></span></header>
<div class="wrap">
<div class="card"><h2>① 选品（%(skw)s）</h2>
<div class="kpi"><div><b>%(scount)s</b><span>竞品样本</span></div>
<div><b>%(smed)s</b><span>价格中位 USD</span></div>
<div><b>%(srate)s</b><span>均分</span></div>
<div><b>%(sopp)s</b><span>差评机会位</span></div></div>
<div class="empty">价格区间 %(smin)s ~ %(smax)s ｜ 平台 %(splat)s ｜ 数据源 %(sfile)s</div>
<div>%(skwchips)s</div></div>

<div class="card"><h2>② 利润测算</h2>
<div class="kpi"><div><b>%(pmargin)s%%</b><span>毛利率</span></div>
<div><b>%(pgross)s</b><span>毛利 USD</span></div>
<div><b>%(proi)s%%</b><span>ROI</span></div>
<div><b>%(pbe)s</b><span>盈亏平衡价</span></div></div>
<table>%(profrows)s</table>
<div class="empty">%(pplat)s ｜ %(pchan)s ｜ 汇率 %(pfx)s ｜ 数据源 %(pfile)s</div></div>

<div class="card"><h2>③ 上架 Listing</h2>
<div class="kpi"><div><b>%(llang)s</b><span>语种数</span></div>
<div><b>%(lkw)s</b><span>关键词数</span></div></div>
<table><tr><th>语种</th><th>标题</th><th class='n'>五点</th></tr>%(langrows)s</table>
<div class="empty">数据源 %(lfile)s</div></div>

<div class="card"><h2>④ 订单 / 询盘</h2>
<div class="kpi"><div><b>%(ocount)s</b><span>询盘数</span></div>
<div><b>A%(oa)s</b><span>A类客户</span></div>
<div><b>%(opipe)s</b><span>线索金额 USD</span></div>
<div><b>%(odue)s</b><span>今日待跟进</span></div></div>
<table><tr><th>ID</th><th>客户</th><th>国</th><th>层</th><th>阶段</th><th class='n'>数量</th><th class='n'>目标价</th><th class='n'>分</th></tr>%(leadrows)s</table>
<div class="empty">数据源 %(ofile)s</div></div>

<div class="card"><h2>⑤ 物流比价</h2>
<div class="kpi"><div><b>%(lrec)s</b><span>综合最优</span></div></div>
<table><tr><th>渠道</th><th class='n'>运费CNY</th><th>时效</th><th>轨迹</th></tr>%(logrows)s</table>
<div class="empty">数据源 %(lfile)s</div></div>

<div class="card"><h2>⑥ 竞品明细</h2>
<table><tr><th>平台</th><th>标题</th><th class='n'>价格</th><th class='n'>评分</th><th class='n'>销量</th></tr>%(itemrows)s</table></div>
</div>
<footer>真实数据，缺项显示"未导入"，不填充示例值。刷新页面即读取最新 JSON。</footer></body></html>""" % {
        "skw": s.get("keyword") or "—", "scount": s.get("count", 0),
        "smed": s.get("price_median") or "—", "srate": s.get("rating_mean") or "—",
        "sopp": s.get("opportunity_count", 0), "smin": s.get("price_min") or "—",
        "smax": s.get("price_max") or "—", "splat": ",".join(s.get("platforms") or []) or "—",
        "sfile": s.get("file") or "未导入", "skwchips": kw or '<div class="empty">无关键词</div>',
        "pmargin": p.get("margin", "—"), "pgross": p.get("gross", "—"),
        "proi": p.get("roi", "—"), "pbe": p.get("breakeven", "—"),
        "profrows": profit_rows or '<div class="empty">未导入</div>',
        "pplat": p.get("platform") or "—", "pchan": p.get("channel") or "—",
        "pfx": p.get("fx") or "—", "pfile": p.get("file") or "未导入",
        "llang": len(l.get("langs") or []), "lkw": l.get("kw_count", 0),
        "langrows": lang_rows or '<div class="empty">未导入</div>',
        "lfile": l.get("file") or "未导入",
        "ocount": o.get("count", 0), "oa": (o.get("tiers") or {}).get("A", 0),
        "opipe": o.get("pipeline_usd", 0), "odue": len(o.get("due") or []),
        "leadrows": lead_rows or '<div class="empty">未导入</div>',
        "ofile": o.get("file") or "未导入",
        "lrec": rec.get("balanced") or "—",
        "logrows": log_rows or '<div class="empty">未导入</div>',
        "lfile2": "", "itemrows": items_rows or '<div class="empty">未导入</div>',
    }


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/" or p == "/index.html":
            self._send(200, render_html(collect()), "text/html; charset=utf-8")
        elif p == "/api/summary":
            d = collect()
            self._send(200, json.dumps({"ok": True, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "data": d}, ensure_ascii=False, indent=2))
        elif p == "/api/pipeline":
            d = collect()
            stages = [{"stage": k,
                       "status": ("ready" if d[k].get("file") else "missing"),
                       "metric": (d[k].get("count") if k in ("选品", "订单")
                                  else (d[k].get("margin") if k == "利润" else
                                        (len(d[k].get("langs") or []) if k == "上架" else
                                         len(d[k].get("rows") or [])))),
                       "file": d[k].get("file")} for k in
                      ["选品", "供应链", "利润", "上架", "订单", "物流"]]
            self._send(200, json.dumps({"ok": True, "pipeline": stages},
                                       ensure_ascii=False, indent=2))
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true", help="只打印汇总不启动服务")
    a = ap.parse_args()
    if a.once:
        print(json.dumps(collect(), ensure_ascii=False, indent=2))
        return
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print("[+] 看板已启动 http://%s:%d/  （Ctrl+C 停止）" % (a.host, a.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[+] 已停止")


if __name__ == "__main__":
    import sys
    main()
