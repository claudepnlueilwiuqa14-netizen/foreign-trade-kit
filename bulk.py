#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
bulk.py —— 多平台批量上架：读 listing.json → 一键生成各平台上架文件

支持平台与产物格式：
  amazon     库存加载器 Flat File CSV（SKU/标题/五点/描述/价格/库存）
  ebay       文件交换 CSV（Title/Price/Quantity/Description/Brand）
  shopify    products.json 载荷 —— **唯一能真实推送的平台**（Admin API，见 push）
  aliexpress JSON 载荷（subject/detail/multi-sku）
  etsy       上架 CSV（标题/描述/价格/数量/标签，13 个 tag 上限）

为什么只有 shopify 能真实推：
  Amazon SP-API / eBay Trading API / Etsy API 都要卖家账号审批 + OAuth，
  本机没有这些凭证，**不造假**——生成规范格式文件，人工在后台上传或后续接 API。
  Shopify Admin API 只要一个自定义 token（shop 域名 + shpat_ token）就能推。

用法：
  python bulk.py plan --listing listing.json --platforms all --price 19.99 --stock 500
  python bulk.py plan --listing listing.json --platforms amazon,ebay --price 24.5 --stock 200
  python bulk.py push --platform shopify --shop mystore.myshopify.com --token-file key.txt
  python bulk.py report
"""
import os
import sys
import json
import csv
import time
import argparse
import datetime
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
PAYDIR = os.path.join(HERE, "payloads")
sys.path.insert(0, HERE)
from listing import PLATFORM_RULE  # noqa: E402

PLATFORMS = ["amazon", "ebay", "shopify", "aliexpress", "etsy"]


def sku_of(product, lang, i):
    base = "".join(ch for ch in product if ch.isalnum())[:6].upper() or "ITEM"
    return "%s-%s-%03d" % (base, lang.upper(), i)


def remap(listing, platform):
    """把 listing 裁成目标平台的规则（标题长度/五点数量/每条长度）。"""
    rule = PLATFORM_RULE.get(platform)
    if not rule:
        return None
    title = (listing.get("title") or "")[:rule["title_max"]]
    bullets = [b[:rule["bullet_max"]] for b in (listing.get("bullets") or [])][:rule["bullets"]]
    kw = (listing.get("search_terms") or "")[:rule["kw_max"]]
    return {"title": title, "bullets": bullets, "search_terms": kw,
            "description": listing.get("description") or "", "lang": listing.get("lang")}


# ---------------------------------------------------------------- 各平台产物
def amazon_csv(items, product, price, stock, path):
    rows = []
    for i, it in enumerate(items, 1):
        row = {"sku": sku_of(product, it["lang"], i),
               "item_name": it["title"],
               "brand": "Generic",
               "product_description": it["description"][:2000],
               "standard_price": price, "quantity": stock,
               "condition_type": "New", "fulfillment_channel": "DEFAULT",
               "search_terms": it["search_terms"]}
        for j, b in enumerate(it["bullets"][:5], 1):
            row["bullet_point_%d" % j] = b
        for j in range(len(it["bullets"]) + 1, 6):
            row["bullet_point_%d" % j] = ""
        rows.append(row)
    cols = ["sku", "item_name", "brand", "bullet_point_1", "bullet_point_2", "bullet_point_3",
            "bullet_point_4", "bullet_point_5", "product_description", "search_terms",
            "standard_price", "quantity", "condition_type", "fulfillment_channel"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def ebay_csv(items, product, price, stock, path):
    rows = []
    for i, it in enumerate(items, 1):
        rows.append({"CustomLabel": sku_of(product, it["lang"], i),
                     "Title": it["title"][:80],
                     "Price": price, "Quantity": stock,
                     "ConditionID": 1000,
                     "Description": "<![CDATA[" + (it["description"][:4000] or it["title"]) + "]]>",
                     "C:Brand": "Generic",
                     "Format": "FixedPrice"})
    cols = ["CustomLabel", "Title", "Price", "Quantity", "ConditionID", "Description", "C:Brand", "Format"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def etsy_csv(items, product, price, stock, path):
    rows = []
    for i, it in enumerate(items, 1):
        tags = [t.strip() for t in (it["search_terms"] or "").split(",") if t.strip()][:13]
        rows.append({"TITLE": it["title"][:140],
                     "DESCRIPTION": (it["description"] or it["title"])[:60000],
                     "PRICE": price, "QUANTITY": stock,
                     "TAGS": ",".join(tags),
                     "WHO_MADE": "collective", "WHEN_MADE": "2020_2026",
                     "IS_SUPPLY": "false", "PROCESSING_TIME": 3,
                     "STYLE": "", "MATERIALS": ""})
    cols = ["TITLE", "DESCRIPTION", "PRICE", "QUANTITY", "TAGS", "WHO_MADE",
            "WHEN_MADE", "IS_SUPPLY", "PROCESSING_TIME", "STYLE", "MATERIALS"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def shopify_json(items, product, price, stock, path):
    prods = []
    for i, it in enumerate(items, 1):
        prods.append({
            "product": {
                "title": it["title"],
                "body_html": "<ul>" + "".join("<li>%s</li>" % b for b in it["bullets"]) + "</ul>",
                "vendor": "Generic",
                "product_type": product,
                "tags": it["search_terms"],
                "variants": [{"sku": sku_of(product, it["lang"], i),
                              "price": "%.2f" % price,
                              "inventory_management": "shopify",
                              "inventory_policy": "deny",
                              "inventory_quantity": stock}],
            }
        })
    json.dump({"products": prods}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return len(prods)


def aliexpress_json(items, product, price, stock, path):
    out = []
    for i, it in enumerate(items, 1):
        out.append({
            "subject": it["title"][:128],
            "detail": {"module": {"description": it["description"] or it["title"],
                                  "bullet_points": it["bullets"]}},
            "sku_list": [{"sku_code": sku_of(product, it["lang"], i),
                          "price": price, "inventory": stock}],
            "language": it["lang"],
        })
    json.dump({"items": out}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return len(out)


WRITERS = {"amazon": amazon_csv, "ebay": ebay_csv, "shopify": shopify_json,
           "aliexpress": aliexpress_json, "etsy": etsy_csv}


def plan(listing_file, platforms, price, stock):
    d = json.load(open(listing_file, encoding="utf-8"))
    product = d.get("product", "item")
    langs = [l for l in d.get("listings", [])]
    if not langs:
        print("[!] %s 里没有 listings" % listing_file); return
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(PAYDIR, "%s_%s" % (stamp, product))
    os.makedirs(base, exist_ok=True)
    print("[*] 产品 %s ｜ %d 个语言版 ｜ 价 %.2f USD ｜ 库存 %d" % (product, len(langs), price, stock))
    made = []
    for p in platforms:
        if p not in WRITERS:
            print("[!] 不支持的平台 %s（可选：%s）" % (p, ",".join(PLATFORMS))); continue
        items = [r for r in (remap(l, p) for l in langs) if r]
        if not items:
            continue
        ext = "json" if p in ("shopify", "aliexpress") else "csv"
        fp = os.path.join(base, "%s_%s.%s" % (p, product, ext))
        n = WRITERS[p](items, product, price, stock, fp)
        made.append((p, fp, n))
        print("[+] %-11s %d 条 -> %s" % (p, n, os.path.relpath(fp, HERE)))
    # 汇总报告
    rp = os.path.join(base, "README.txt")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("多平台上架产物 · %s\n" % stamp)
        f.write("产品：%s ｜ 价格 %.2f ｜ 库存 %d ｜ 来源 %s\n\n" % (product, price, stock, listing_file))
        for p, fp, n in made:
            f.write("- %-11s %3d 条  %s\n" % (p, n, fp))
        f.write("\n上传方式：\n")
        f.write("  amazon     卖家后台 > 库存 > 批量上传商品 > 选本 CSV\n")
        f.write("  ebay       卖家中心 > 文件交换 > 上传 CSV\n")
        f.write("  shopify    后台 > 产品 > 导入，或 python bulk.py push --platform shopify ...\n")
        f.write("  aliexpress 卖家后台 > 商品批量发布 > 按字段粘贴\n")
        f.write("  etsy       后台 > 设置 > 上架管理 > 批量 CSV\n")
    print("\n[✓] 全部产物在 %s" % os.path.relpath(base, HERE))
    print("    汇总：%s" % os.path.relpath(rp, HERE))
    return base


def push(platform, shop, token_file, listing_file, price, stock):
    """真实推送。目前只支持 Shopify Admin API。"""
    if platform != "shopify":
        print("[!] push 目前只支持 shopify（其余平台需卖家账号 OAuth 审批，未接入）")
        print("    用 plan 生成规范文件，到各平台后台上传。")
        return
    if not os.path.exists(token_file):
        print("[!] token 文件不存在：%s" % token_file)
        print("    获取：Shopify 后台 > 设置 > 应用和销售渠道 > 开发应用 > Admin API > 暴露 write_products > 安装 > 复制 Admin API access token")
        return
    token = open(token_file, encoding="utf-8").read().strip()
    d = json.load(open(listing_file, encoding="utf-8"))
    product = d.get("product", "item")
    items = [remap(l, "shopify") for l in d.get("listings", [])]
    items = [r for r in items if r]
    ok = fail = 0
    for i, it in enumerate(items, 1):
        body = json.dumps({"product": {
            "title": it["title"],
            "body_html": "<ul>" + "".join("<li>%s</li>" % b for b in it["bullets"]) + "</ul>",
            "vendor": "Generic", "product_type": product, "tags": it["search_terms"],
            "variants": [{"sku": sku_of(product, it["lang"], i), "price": "%.2f" % price,
                          "inventory_management": "shopify", "inventory_quantity": stock}],
        }}).encode()
        url = "https://%s/admin/api/2026-01/products.json" % shop
        r = urllib.request.Request(url, data=body, method="POST", headers={
            "X-Shopify-Access-Token": token, "Content-Type": "application/json",
            "User-Agent": "bulk.py/1.0"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                resp.read()
                ok += 1
                print("[+] %s 已创建" % it["title"][:50])
        except urllib.error.HTTPError as e:
            fail += 1
            print("[x] %s -> %s %s" % (it["title"][:40], e.code, e.read()[:200]))
        except Exception as e:
            fail += 1
            print("[x] %s -> %s" % (it["title"][:40], str(e)[:120]))
        time.sleep(0.6)   # Shopify 2 req/s 限流
    print("\n[✓] 成功 %d / 失败 %d（%s）" % (ok, fail, shop))


def report():
    if not os.path.isdir(PAYDIR):
        print("还没有产物，先跑 plan"); return
    dirs = sorted(os.listdir(PAYDIR))
    for dn in dirs:
        fp = os.path.join(PAYDIR, dn)
        if not os.path.isdir(fp):
            continue
        print("\n== %s ==" % dn)
        for f in sorted(os.listdir(fp)):
            p = os.path.join(fp, f)
            n = "-"
            if f.endswith(".csv"):
                try:
                    n = "%d 行" % (sum(1 for _ in open(p, encoding="utf-8-sig")) - 1)
                except Exception:
                    pass
            elif f.endswith(".json"):
                try:
                    n = "%d 条" % len(json.load(open(p, encoding="utf-8")).get("products",
                        json.load(open(p, encoding="utf-8")).get("items", [])))
                except Exception:
                    pass
            print("   %-40s %8s" % (f, n))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    pl = sub.add_parser("plan")
    pl.add_argument("--listing", default="listing.json")
    pl.add_argument("--platforms", default="all")
    pl.add_argument("--price", type=float, default=19.99)
    pl.add_argument("--stock", type=int, default=500)
    ps = sub.add_parser("push")
    ps.add_argument("--platform", default="shopify", choices=["shopify"])
    ps.add_argument("--shop", required=True, help="xxx.myshopify.com")
    ps.add_argument("--token-file", required=True)
    ps.add_argument("--listing", default="listing.json")
    ps.add_argument("--price", type=float, default=19.99)
    ps.add_argument("--stock", type=int, default=500)
    sub.add_parser("report")
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help(); return
    if a.cmd == "plan":
        plats = PLATFORMS if a.platforms == "all" else [x.strip() for x in a.platforms.split(",") if x.strip()]
        plan(a.listing, plats, a.price, a.stock)
    elif a.cmd == "push":
        push(a.platform, a.shop, a.token_file, a.listing, a.price, a.stock)
    elif a.cmd == "report":
        report()


if __name__ == "__main__":
    main()
