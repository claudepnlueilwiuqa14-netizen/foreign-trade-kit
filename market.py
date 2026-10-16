#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
market.py —— 选品与竞品数据采集（Amazon / eBay / AliExpress / Shopify）

实测结论（2026-10-16，本机 127.0.0.1:7897 出口）：
  * Amazon 搜索页：requests 直连必回 503（"To discuss automated access…"），
    **必须**用真实浏览器渲染（playwright）才能拿到商品卡片。
  * eBay 搜索页：requests 403；ebay.com 对当前出口 IP 直接跳 Error Page，
    需多区域域名（com / co.uk / de）重试，仍失败时如实标记不可用。
  * AliExpress / Alibaba：requests 直接 200 有正文，可从内嵌 JSON 提取。
  * Shopify：任意 Shopify 站都有 /products.json 公开接口（部分站会 403，
    属站点自身限制，不是本工具的问题）。

用法：
  python market.py search "wireless earbuds" --platforms amazon,ebay,aliexpress --max 12
  python market.py search "yoga mat" --platforms shopify --shop allbirds
  python market.py analyze market_data.json          # 竞品价格/评分分布分析
输出：控制台表 + market_data.json / market_data.csv
"""
import os
import re
import csv
import sys
import json
import time
import random
import argparse
import statistics
from urllib.parse import quote

PROXY = os.environ.get("TRADE_PROXY", "http://127.0.0.1:7897")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = float(os.environ.get("TRADE_TIMEOUT", "25"))

try:
    import requests
except Exception:
    requests = None


# ---------------------------------------------------------------- HTTP 层
def http_get(url, headers=None, timeout=None):
    hdr = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    if headers:
        hdr.update(headers)
    t0 = time.time()
    try:
        if requests is not None:
            r = requests.get(url, headers=hdr, timeout=timeout or TIMEOUT,
                             proxies={"http": PROXY, "https": PROXY})
            return {"status": r.status_code, "text": r.text, "url": r.url,
                    "ms": int((time.time() - t0) * 1000), "err": None}
        import urllib.request
        req = urllib.request.Request(url, headers=hdr)
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        r = op.open(req, timeout=timeout or TIMEOUT)
        return {"status": r.status, "text": r.read().decode("utf-8", "ignore"),
                "url": r.geturl(), "ms": int((time.time() - t0) * 1000), "err": None}
    except Exception as e:
        return {"status": getattr(e, "code", 0), "text": "", "url": url,
                "ms": int((time.time() - t0) * 1000), "err": str(e)[:90]}


def _num(s):
    if s is None:
        return None
    m = re.search(r"(\d[\d,]*\.?\d*)", str(s).replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _sold_estimate(text):
    """从卡片文本里抓销量线索（Amazon: '10K+ bought in past month'；
    eBay: '123 sold'；AliExpress: '456 orders'）。"""
    t = (text or "").lower()
    m = re.search(r"([\d.,]+)\s*k\s*\+?\s*bought", t)
    if m:
        return int(_num(m.group(1)) * 1000)
    m = re.search(r"([\d,]+)\+?\s*(?:sold|orders|已售)", t)
    if m:
        return int(_num(m.group(1)) or 0)
    return None


# ---------------------------------------------------------------- Amazon
def _amazon_render(kw, maxn):
    """Amazon 必须真浏览器：requests 直连是 503。"""
    from playwright.sync_api import sync_playwright
    url = "https://www.amazon.com/s?k=" + quote(kw)
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, proxy={"server": PROXY})
        ctx = b.new_context(user_agent=UA, locale="en-US")
        pg = ctx.new_page()
        pg.goto(url, timeout=60000, wait_until="domcontentloaded")
        pg.wait_for_timeout(3000)
        try:
            pg.wait_for_selector("div[data-component-type='s-search-result']", timeout=15000)
        except Exception:
            pass
        cards = pg.query_selector_all("div[data-component-type='s-search-result']")[:maxn]
        for c in cards:
            txt = c.inner_text()
            asin = c.get_attribute("data-asin") or ""
            title = ""
            h2 = c.query_selector("h2")
            if h2:
                title = h2.inner_text().strip()
            # Amazon 按出口 IP 切站点货币：日本出口下价格是 "¥4,990"（日元），
            # 直接当 USD 会把 4990 日元记成 4990 美元。必须识别符号再换算。
            price, cur = None, "USD"
            for sel in ("span.a-price span.a-offscreen", "span.a-price-whole"):
                el = c.query_selector(sel)
                if el:
                    ptxt = el.inner_text()
                    price = _num(ptxt)
                    if price:
                        if "¥" in ptxt or "￥" in ptxt or "JPY" in ptxt.upper():
                            cur = "JPY"
                        elif "£" in ptxt:
                            cur = "GBP"
                        elif "€" in ptxt:
                            cur = "EUR"
                        break
            rating = reviews = None
            el = c.query_selector("span.a-icon-alt")
            if el:
                rating = _num(el.inner_text())
            for el in c.query_selector_all("span.a-size-base.s-underline-text, span.s-underline-text"):
                v = _num(el.inner_text())
                if v:
                    reviews = int(v)
                    break
            link = ""
            a = c.query_selector("a.a-link-normal.s-line-clamp-2, h2 a, a")
            if a:
                href = a.get_attribute("href") or ""
                link = "https://www.amazon.com" + href if href.startswith("/") else href
            if cur != "USD":
                try:
                    from sourcing import get_fx
                    fx = get_fx()
                    key = {"JPY": "JPYCNY", "GBP": "GBPCNY", "EUR": "EURCNY"}[cur]
                    price = round(price * fx[key] / fx["USDCNY"], 2)
                    cur = "USD(由%s换算)" % cur
                except Exception:
                    pass
            out.append({
                "platform": "amazon", "asin": asin, "title": title[:180],
                "price": price, "currency": cur, "rating": rating,
                "reviews": reviews, "sold_estimate": _sold_estimate(txt),
                "url": link.split("?")[0], "raw": txt[:220],
            })
        b.close()
    return out


# ---------------------------------------------------------------- eBay
def _ebay_render(kw, maxn):
    """eBay 对部分出口 IP 直给 Error Page，逐个区域域名重试，全失败则回报空。"""
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, proxy={"server": PROXY})
        ctx = b.new_context(user_agent=UA, locale="en-US")
        pg = ctx.new_page()
        for dom in ("https://www.ebay.com", "https://www.ebay.co.uk", "https://www.ebay.de"):
            url = "%s/sch/i.html?_nkw=%s" % (dom, quote(kw))
            try:
                pg.goto(url, timeout=45000, wait_until="domcontentloaded")
                pg.wait_for_timeout(2500)
                if "Error Page" in (pg.title() or ""):
                    continue
                cards = pg.query_selector_all("li.s-item")
                if not cards:
                    continue
                for c in cards[:maxn]:
                    txt = c.inner_text()
                    title = ""
                    el = c.query_selector(".s-item__title")
                    if el:
                        title = el.inner_text().strip()
                    if title.lower().startswith("shop on ebay"):
                        continue
                    price = _num((c.query_selector(".s-item__price").inner_text()
                                  if c.query_selector(".s-item__price") else ""))
                    sold = None
                    el = c.query_selector(".s-item__hotness, .s-item__dynamic")
                    if el:
                        sold = _sold_estimate(el.inner_text())
                    href = ""
                    a = c.query_selector("a")
                    if a:
                        href = a.get_attribute("href") or ""
                    out.append({
                        "platform": "ebay", "title": title[:180], "price": price,
                        "currency": "USD" if dom.endswith("com") else ("GBP" if "co.uk" in dom else "EUR"),
                        "rating": None, "reviews": None,
                        "sold_estimate": sold or _sold_estimate(txt),
                        "url": href.split("?")[0], "raw": txt[:200],
                    })
                if out:
                    break
            except Exception:
                continue
        b.close()
    return out


# ---------------------------------------------------------------- AliExpress
def _aliexpress_render(kw, maxn):
    """AliExpress 搜索页是前端渲染的：requests 拿到的 HTML 里商品是空的，
    必须用真浏览器。实测可拿到 9+ 条真实商品卡片（a[href*='/item/']）。"""
    from playwright.sync_api import sync_playwright
    url = "https://www.aliexpress.com/w/wholesale-%s.html" % quote(kw.replace(" ", "-"))
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, proxy={"server": PROXY})
        ctx = b.new_context(user_agent=UA, locale="en-US")
        pg = ctx.new_page()
        pg.goto(url, timeout=60000, wait_until="domcontentloaded")
        try:
            pg.wait_for_selector("a[href*='/item/']", timeout=20000)
        except Exception:
            pass
        pg.wait_for_timeout(2500)
        for a in pg.query_selector_all("a[href*='/item/']")[:maxn * 3]:
            txt = (a.inner_text() or "").strip().replace("\n", " ")
            if len(txt) < 15:
                continue
            # AliExpress 按出口 IP 自动切语言，日本出口会渲染成日语页并把价格标成日元。
            # 不区分币种直接当 USD，会把 3627 日元当成 3627 美元，整个价格分布被污染。
            cur, price = "USD", None
            m = re.search(r"US\s*\$\s*([\d,]+\.?\d*)", txt) or re.search(r"\$\s*([\d,]+\.?\d*)", txt)
            if m:
                price = _num(m.group(1))
            else:
                m = re.search(r"[¥￥]\s*([\d,]+)", txt)
                if m:
                    cur, price = "JPY", _num(m.group(1))
                    try:
                        from sourcing import get_fx
                        fx = get_fx()
                        price = round(price * fx["JPYCNY"] / fx["USDCNY"], 2)
                        cur = "USD(由JPY换算)"
                    except Exception:
                        pass
            href = a.get_attribute("href") or ""
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://www.aliexpress.com" + href
            out.append({"platform": "aliexpress", "title": txt[:180], "price": price,
                        "currency": cur, "rating": None, "reviews": None,
                        "sold_estimate": _sold_estimate(txt), "url": href.split("?")[0],
                        "raw": txt[:180]})
            if len(out) >= maxn:
                break
        b.close()
    return out


def _aliexpress_requests(kw, maxn):
    url = "https://www.aliexpress.com/w/wholesale-%s.html" % quote(kw.replace(" ", "-"))
    r = http_get(url, timeout=30)
    if r["status"] != 200:
        return []
    out = []
    # 注意：早期版本用 `\{"[^{}]*?"title":"(.*?)".*?"price"...\}` 一次性匹配整段 JSON，
    # 在 60KB 的 HTML 上会触发灾难性回溯，进程直接挂死十几分钟。
    # 这里改成两条线性正则（无嵌套量词），各自独立抓。
    titles = [re.sub(r"\\u[0-9a-fA-F]{4}", "", t)
              for t in re.findall(r'"title":"([^"\\]{8,150})"', r["text"])]
    prices = re.findall(r'"(?:formattedPrice|minPrice|price)"\s*:\s*"?([\d]+(?:[.,]\d+)?)"?',
                        r["text"])
    if not prices:
        prices = re.findall(r'US\s*\$\s*([\d,]+\.?\d*)', r["text"])
    seen = set()
    for i, t in enumerate(titles):
        if t in seen or len(t) < 10:
            continue
        seen.add(t)
        out.append({"platform": "aliexpress", "title": t[:180],
                    "price": _num(prices[i]) if i < len(prices) else None,
                    "currency": "USD", "rating": None, "reviews": None,
                    "sold_estimate": None, "url": url, "raw": ""})
        if len(out) >= maxn:
            break
    return out


def _alibaba_requests(kw, maxn):
    url = "https://www.alibaba.com/trade/search?SearchText=" + quote(kw)
    r = http_get(url, timeout=30)
    if r["status"] != 200:
        return []
    out = []
    titles = re.findall(r'"title":"([^"]{10,150})"', r["text"])
    prices = re.findall(r'"price"\s*:\s*"?([\d.]+)"?', r["text"])
    seen = set()
    for i, t in enumerate(titles):
        if t in seen:
            continue
        seen.add(t)
        out.append({"platform": "alibaba", "title": t[:180],
                    "price": _num(prices[i]) if i < len(prices) else None,
                    "currency": "USD", "rating": None, "reviews": None,
                    "sold_estimate": None, "url": url, "raw": ""})
        if len(out) >= maxn:
            break
    return out


# ---------------------------------------------------------------- Shopify
def _shopify(shop, maxn):
    url = "https://%s/products.json?limit=%d" % (shop, maxn)
    r = http_get(url, headers={"accept": "application/json"})
    if r["status"] != 200:
        return []
    try:
        j = json.loads(r["text"])
    except Exception:
        return []
    out = []
    for p in j.get("products", [])[:maxn]:
        v = (p.get("variants") or [{}])[0]
        out.append({
            "platform": "shopify:" + shop, "title": (p.get("title") or "")[:180],
            "price": _num(v.get("price")), "currency": "USD",
            "rating": None, "reviews": None, "sold_estimate": None,
            "url": "https://%s/products/%s" % (shop, p.get("handle") or ""),
            "raw": (p.get("product_type") or ""), "sku": v.get("sku"),
            "inventory": v.get("inventory_quantity"),
        })
    return out


# ---------------------------------------------------------------- 调度
def search(kw, platforms=("amazon", "ebay", "aliexpress"), maxn=12, shop=None):
    result, notes = [], []
    for pf in platforms:
        try:
            if pf == "amazon":
                rows = _amazon_render(kw, maxn)
            elif pf == "ebay":
                rows = _ebay_render(kw, maxn)
            elif pf == "aliexpress":
                rows = _aliexpress_render(kw, maxn)
                if not rows:
                    rows = _aliexpress_requests(kw, maxn)
            elif pf == "alibaba":
                rows = _alibaba_requests(kw, maxn)
            elif pf == "shopify":
                if not shop:
                    notes.append("shopify: 未提供 --shop，跳过")
                    continue
                rows = _shopify(shop, maxn)
            else:
                notes.append("%s: 不支持的平台" % pf)
                continue
            result.extend(rows)
            notes.append("%s: %d 条%s" % (pf, len(rows), "" if rows else "（本次未取到，见 README 已知限制）"))
        except Exception as e:
            notes.append("%s: 失败 %s: %s" % (pf, type(e).__name__, str(e)[:70]))
    return result, notes


def analyze(rows):
    """竞品分析：价格分位、评分分布、标题高频词、机会窗口。"""
    prices = [r["price"] for r in rows if r.get("price")]
    ratings = [r["rating"] for r in rows if r.get("rating")]
    sold = [r["sold_estimate"] for r in rows if r.get("sold_estimate")]
    words = {}
    for r in rows:
        for w in re.findall(r"[a-zA-Z]{3,}", (r.get("title") or "").lower()):
            words[w] = words.get(w, 0) + 1
    stop = {"the", "and", "for", "with", "you", "your", "new", "set", "pack",
            "wireless", "black", "white", "pro", "plus", "best"}
    top = sorted([(k, v) for k, v in words.items() if k not in stop],
                 key=lambda x: -x[1])[:15]
    a = {"count": len(rows), "with_price": len(prices)}
    if prices:
        ps = sorted(prices)
        a.update({"price_min": ps[0], "price_max": ps[-1],
                  "price_median": round(statistics.median(ps), 2),
                  "price_p25": round(ps[max(0, len(ps) // 4)], 2),
                  "price_p75": round(ps[min(len(ps) - 1, len(ps) * 3 // 4)], 2),
                  "price_mean": round(statistics.mean(ps), 2)})
    if ratings:
        a.update({"rating_mean": round(statistics.mean(ratings), 2),
                  "rating_min": min(ratings), "low_rating_share": round(
                      sum(1 for x in ratings if x < 4.0) / len(ratings), 3)})
    if sold:
        a.update({"sold_max": max(sold), "sold_median": int(statistics.median(sold))})
    a["top_keywords"] = top
    # 机会窗口：评分差但销量高的品 = 差异化机会；价格分层空档
    a["opportunity"] = {
        "low_rating_high_sold": [
            {"title": r["title"][:70], "rating": r.get("rating"), "sold": r.get("sold_estimate")}
            for r in rows
            if (r.get("rating") or 5) < 4.3 and (r.get("sold_estimate") or 0) > 0
        ][:5],
        "note": "评分区间 <4.3 且有销量 = 现存差评痛点，是差异化卖点与 Review 突破位",
    }
    return a


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("search")
    s.add_argument("kw")
    s.add_argument("--platforms", default="amazon,ebay,aliexpress")
    s.add_argument("--max", type=int, default=12)
    s.add_argument("--shop")
    s.add_argument("--out", default="market_data.json")
    a2 = sub.add_parser("analyze")
    a2.add_argument("file")
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return

    if args.cmd == "search":
        t0 = time.time()
        rows, notes = search(args.kw, [x.strip() for x in args.platforms.split(",")],
                             args.max, args.shop)
        print("[*] 关键词 %r，耗时 %.1fs" % (args.kw, time.time() - t0))
        for n in notes:
            print("    " + n)
        print("\n%-10s %-52s %9s %6s %8s %10s" %
              ("平台", "标题", "价格", "评分", "评论", "销量估算"))
        print("-" * 104)
        for r in rows:
            print("%-10s %-52s %9s %6s %8s %10s" % (
                str(r["platform"])[:10], (r["title"] or "")[:52],
                r["price"] if r["price"] is not None else "-",
                r["rating"] or "-", r["reviews"] or "-", r["sold_estimate"] or "-"))
        print("-" * 104)
        print("共 %d 条" % len(rows))
        d = {"keyword": args.kw, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
             "notes": notes, "items": rows, "analysis": analyze(rows)}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        with open(args.out.replace(".json", ".csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["platform", "title", "price", "currency",
                                              "rating", "reviews", "sold_estimate", "url"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in w.fieldnames})
        print("[+] 已写 %s / .csv" % args.out)
        an = d["analysis"]
        print("\n竞品分析：价格中位 %s，区间 %s~%s，均价 %s" % (
            an.get("price_median"), an.get("price_min"), an.get("price_max"), an.get("price_mean")))
        print("标题高频词：%s" % ", ".join("%s(%d)" % (k, v) for k, v in an["top_keywords"][:10]))
    else:
        d = json.load(open(args.file, encoding="utf-8"))
        rows = d.get("items", d)
        print(json.dumps(analyze(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
