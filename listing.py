#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
listing.py —— 多平台 Listing 生成（标题 / 五点 / 描述 / 关键词）+ 多语言 + SEO 挖掘

两条生成通道：
  1) --engine template（默认，离线可跑）：基于竞品真实标题做关键词挖掘，
     再按各平台规则拼装。适合批量铺货。
  2) --engine ai：调用大模型产出文案（端点 https://www.985la.cn/v1，
     key 从 --ai-key-file 或环境变量 AI_API_KEY 读，**绝不写进仓库**）。
     拿不到 key 就自动回落 template 并明确提示，不静默造假。

支持语言：中 zh / 英 en / 日 ja / 德 de / 法 fr / 西 es

用法：
  python listing.py gen --market demo_market.json --product "无线蓝牙耳机" --lang en,ja,de
  python listing.py gen --market demo_market.json --platform amazon --engine ai \
      --ai-key-file C:/path/key.txt --out listing.json
  python listing.py keywords --market demo_market.json     # 只看 SEO 关键词
"""
import os
import re
import io
import sys
import json
import time
import argparse
import urllib.request
from collections import Counter
from urllib.parse import quote

PROXY = os.environ.get("TRADE_PROXY", "http://127.0.0.1:7897")
AI_ENDPOINT = os.environ.get("AI_ENDPOINT", "https://www.985la.cn/v1")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"

# ---------------------------------------------------------------- 平台规则
PLATFORM_RULE = {
    "amazon": {"title_max": 200, "bullets": 5, "bullet_max": 255, "kw_max": 250,
               "title_fmt": "{brand} {core} - {attrs} for {scene}, {benefit}"},
    "ebay": {"title_max": 80, "bullets": 4, "bullet_max": 120, "kw_max": 80,
             "title_fmt": "{core} {attrs} {scene}"},
    "aliexpress": {"title_max": 128, "bullets": 4, "bullet_max": 140, "kw_max": 200,
                   "title_fmt": "{core} {attrs} {benefit} for {scene}"},
    "shopify": {"title_max": 120, "bullets": 5, "bullet_max": 200, "kw_max": 320,
                "title_fmt": "{core} — {attrs}"},
}

# ---------------------------------------------------------------- 多语言词典
# 结构：概念 -> {lang: [候选词]}，用于模板拼装（离线通道）
DICT = {
    "无线": {"en": ["Wireless"], "ja": ["ワイヤレス"], "de": ["Kabellos"],
             "fr": ["Sans fil"], "es": ["Inalámbrico"], "zh": ["无线"]},
    "蓝牙耳机": {"en": ["Bluetooth Earbuds", "Bluetooth Headphones"],
                 "ja": ["Bluetooth イヤホン"], "de": ["Bluetooth-Kopfhörer"],
                 "fr": ["Écouteurs Bluetooth"], "es": ["Auriculares Bluetooth"], "zh": ["蓝牙耳机"]},
    "降噪": {"en": ["Noise Cancelling"], "ja": ["ノイズキャンセリング"], "de": ["Geräuschunterdrückung"],
             "fr": ["Réduction de bruit"], "es": ["Cancelación de ruido"], "zh": ["降噪"]},
    "防水": {"en": ["Waterproof", "IPX7 Water Resistant"], "ja": ["防水"], "de": ["Wasserdicht"],
             "fr": ["Étanche"], "es": ["Impermeable"], "zh": ["防水"]},
    "长续航": {"en": ["Long Battery Life", "Up to 50H Playtime"], "ja": ["長時間バッテリー"],
               "de": ["Lange Akkulaufzeit"], "fr": ["Longue autonomie"],
               "es": ["Larga duración de batería"], "zh": ["长续航", "超长待机"]},
    "高清音质": {"en": ["HiFi Stereo Sound", "Deep Bass"], "ja": ["高音質"], "de": ["HiFi-Stereoklang"],
                 "fr": ["Son stéréo HiFi"], "es": ["Sonido estéreo HiFi"], "zh": ["高清音质"]},
    "便携": {"en": ["Portable", "Compact"], "ja": ["ポータブル"], "de": ["Tragbar"],
             "fr": ["Portable"], "es": ["Portátil"], "zh": ["便携"]},
    "适用": {"en": ["for", "Compatible with"], "ja": ["対応"], "de": ["für"],
             "fr": ["pour"], "es": ["para"], "zh": ["适用于"]},
    "运动": {"en": ["Sports", "Workout"], "ja": ["スポーツ"], "de": ["Sport"],
             "fr": ["Sport"], "es": ["Deportes"], "zh": ["运动"]},
    "通话": {"en": ["Hands-free Calling"], "ja": ["ハンズフリー通話"], "de": ["Freisprechen"],
             "fr": ["Appels mains libres"], "es": ["Llamadas manos libres"], "zh": ["通话"]},
    "快充": {"en": ["Fast Charging", "Type-C Quick Charge"], "ja": ["急速充電"],
             "de": ["Schnellladung"], "fr": ["Charge rapide"], "es": ["Carga rápida"], "zh": ["快充"]},
}

# 句式模板（离线通道）：{core} {attrs} {benefit} {scene}
BULLET_TEMPLATES = {
    "en": ["【{kw}】{feat} — engineered for {scene}.",
           "{feat} delivers {benefit} so you can {scene} without compromise.",
           "Built with {feat}: {benefit}, tested for {scene}.",
           "Why choose it: {feat} + {benefit}, ideal for {scene}.",
           "{feat} and {benefit} — everything you need for {scene}."],
    "ja": ["【{kw}】{feat}で、{scene}に最適。",
           "{feat}により{benefit}を実現。{scene}でも安心。",
           "{feat}設計：{benefit}、{scene}対応。",
           "選ばれる理由：{feat}と{benefit}。{scene}にぴったり。",
           "{feat}と{benefit}で、{scene}を快適に。"],
    "de": ["【{kw}】{feat} — entwickelt für {scene}.",
           "{feat} sorgt für {benefit}, ideal beim {scene}.",
           "Mit {feat}: {benefit}, geprüft für {scene}.",
           "Warum dieses Produkt: {feat} + {benefit} für {scene}.",
           "{feat} und {benefit} — alles für {scene}."],
    "fr": ["【{kw}】{feat} — conçu pour {scene}.",
           "{feat} offre {benefit}, idéal pour {scene}.",
           "Avec {feat} : {benefit}, testé pour {scene}.",
           "Pourquoi le choisir : {feat} + {benefit} pour {scene}.",
           "{feat} et {benefit} — tout pour {scene}."],
    "es": ["【{kw}】{feat} — diseñado para {scene}.",
           "{feat} ofrece {benefit}, ideal para {scene}.",
           "Con {feat}: {benefit}, probado para {scene}.",
           "¿Por qué elegirlo? {feat} + {benefit} para {scene}.",
           "{feat} y {benefit} — todo para {scene}."],
    "zh": ["【{kw}】{feat}，专为{scene}设计。",
           "{feat}带来{benefit}，{scene}也能从容应对。",
           "采用{feat}：{benefit}，通过{scene}场景测试。",
           "选它的理由：{feat} + {benefit}，{scene}首选。",
           "{feat}与{benefit}兼具，满足{scene}全部需求。"],
}

SCENE = {"en": ["daily commute", "workout", "travel", "home office", "gaming"],
         "ja": ["通勤", "ワークアウト", "旅行", "テレワーク", "ゲーム"],
         "de": ["Pendeln", "Training", "Reisen", "Homeoffice", "Gaming"],
         "fr": ["trajets quotidiens", "sport", "voyage", "télétravail", "jeux"],
         "es": ["trayectos diarios", "entrenamiento", "viajes", "teletrabajo", "juegos"],
         "zh": ["通勤", "运动", "旅行", "居家办公", "游戏"]}

BENEFIT = {"en": ["all-day comfort", "crystal-clear calls", "zero dropouts", "true stereo separation"],
           "ja": ["一日中快適", "クリアな通話", "途切れない接続", "臨場感あるステレオ"],
           "de": ["ganztägiger Komfort", "klare Anrufe", "stabile Verbindung", "echter Stereoklang"],
           "fr": ["confort toute la journée", "appels clairs", "connexion stable", "véritable stéréo"],
           "es": ["comodidad todo el día", "llamadas nítidas", "conexión estable", "verdadero estéreo"],
           "zh": ["全天舒适", "清晰通话", "连接不断连", "真正的立体声"]}

LANG_NAME = {"zh": "中文", "en": "English", "ja": "日本語", "de": "Deutsch",
             "fr": "Français", "es": "Español"}

STOP = set("""the a an and or for with you your our their of to in on at is are be this that
new best free set pack lot pcs piece pieces us uk shipping from plus pro mini max""".split())


# ---------------------------------------------------------------- SEO 关键词挖掘
def mine_keywords(market_file=None, items=None, topn=25):
    if items is None:
        items = json.load(open(market_file, encoding="utf-8")).get("items", [])
    words, bigrams = Counter(), Counter()
    for r in items:
        t = (r.get("title") or "").lower()
        toks = [w for w in re.findall(r"[a-z][a-z\-]{2,}", t) if w not in STOP]
        words.update(toks)
        bigrams.update(" ".join(toks[i:i + 2]) for i in range(len(toks) - 1))
    uni = [(w, c) for w, c in words.most_common(topn)]
    bi = [(b, c) for b, c in bigrams.most_common(topn) if c >= 2]
    return {"unigrams": uni, "bigrams": bi,
            "source_count": len(items),
            "long_tail": ["%s for %s" % (b, s) for (b, _), s in
                          zip(bi[:8], ["travel", "workout", "gaming", "office", "running",
                                       "kids", "iphone", "android"])]}


# ---------------------------------------------------------------- 离线模板生成
def _dict_word(concept, lang, i=0):
    d = DICT.get(concept, {}).get(lang)
    if not d:
        return concept
    return d[i % len(d)]


def gen_template(product, lang, platform, kw_pack, brand="Generic"):
    rule = PLATFORM_RULE.get(platform, PLATFORM_RULE["amazon"])
    core = product
    kws = [k for k, _ in kw_pack["unigrams"][:8]]
    # 标题
    if lang == "en":
        attrs = ", ".join(kws[:4]) or "high quality"
        title = "%s %s - %s for %s" % (brand, core, attrs, SCENE["en"][0])
    elif lang == "zh":
        title = "%s %s %s %s" % (brand, core, " ".join(kws[:3]), "适用多场景")
    else:
        title = "%s %s %s" % (core, " ".join(kws[:3]), _dict_word("降噪", lang))
    title = title.strip()[:rule["title_max"]]
    # 五点
    bullets = []
    tmpl = BULLET_TEMPLATES.get(lang, BULLET_TEMPLATES["en"])
    for i in range(rule["bullets"]):
        kw = kws[i] if i < len(kws) else (product if lang != "en" else "premium quality")
        feat = _dict_word("长续航" if i % 3 == 0 else ("降噪" if i % 3 == 1 else "高清音质"), lang, i)
        benefit = BENEFIT.get(lang, BENEFIT["en"])[i % len(BENEFIT.get(lang, BENEFIT["en"]))]
        scene = SCENE.get(lang, SCENE["en"])[i % len(SCENE.get(lang, SCENE["en"]))]
        b = tmpl[i % len(tmpl)].format(kw=kw, feat=feat, benefit=benefit, scene=scene)
        bullets.append(b[:rule["bullet_max"]])
    # 描述
    desc = " ".join(bullets) if lang == "en" else "".join(bullets)
    search_terms = " ".join([k for k, _ in kw_pack["unigrams"][:20] if k])[:rule["kw_max"]]
    return {"lang": lang, "lang_name": LANG_NAME.get(lang, lang), "platform": platform,
            "title": title, "title_len": len(title), "bullets": bullets, "description": desc,
            "search_terms": search_terms, "engine": "template"}


# ---------------------------------------------------------------- AI 通道
def _post_json(url, payload, headers, timeout=90):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return json.loads(op.open(req, timeout=timeout).read().decode("utf-8"))


def gen_ai(product, lang, platform, kw_pack, api_key, model="gpt-6-astra", brand="Generic"):
    rule = PLATFORM_RULE.get(platform, PLATFORM_RULE["amazon"])
    kws = ", ".join(k for k, _ in kw_pack["unigrams"][:22])
    prompt = (
        "你是资深跨境电商文案。为商品「%s」写 %s 语 %s Listing。\n"
        "必须遵守：标题≤%d字符；五点描述 %d 条，每条≤%d字符；不要用固定开场白、不要重复历史模板；"
        "卖点要具体（材质/参数/场景），禁止空话。\n"
        "参考真实竞品高频词：%s\n"
        "只输出 JSON：{\"title\":...,\"bullets\":[...],\"description\":...,\"search_terms\":...}"
        % (product, LANG_NAME.get(lang, lang), platform, rule["title_max"],
           rule["bullets"], rule["bullet_max"], kws)
    )
    r = _post_json(AI_ENDPOINT + "/chat/completions",
                   {"model": model, "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7},
                   {"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                    "User-Agent": UA, "HTTP-Referer": "https://wenzetongxun.com"})
    txt = (((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise RuntimeError("模型未返回 JSON：%s" % txt[:120])
    j = json.loads(m.group(0))
    return {"lang": lang, "lang_name": LANG_NAME.get(lang, lang), "platform": platform,
            "title": (j.get("title") or "")[:rule["title_max"]],
            "title_len": len(j.get("title") or ""),
            "bullets": [b[:rule["bullet_max"]] for b in (j.get("bullets") or [])][:rule["bullets"]],
            "description": j.get("description") or "",
            "search_terms": (j.get("search_terms") or "")[:rule["kw_max"]],
            "engine": "ai", "model": model}


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gen")
    g.add_argument("--market", default="market_data.json")
    g.add_argument("--product", default="无线蓝牙耳机")
    g.add_argument("--lang", default="zh,en,ja,de,fr,es")
    g.add_argument("--platform", default="amazon", choices=list(PLATFORM_RULE))
    g.add_argument("--engine", default="template", choices=["template", "ai"])
    g.add_argument("--ai-key-file"); g.add_argument("--ai-model", default="gpt-6-astra")
    g.add_argument("--brand", default="Generic")
    g.add_argument("--out", default="listing.json")
    k = sub.add_parser("keywords"); k.add_argument("--market", default="market_data.json")
    k.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help(); return

    if a.cmd == "keywords":
        kw = mine_keywords(a.market, topn=a.top)
        print("竞品样本 %d 条" % kw["source_count"])
        print("\n单词：")
        for w, c in kw["unigrams"]:
            print("  %-22s %d" % (w, c))
        print("\n双词组合：")
        for b, c in kw["bigrams"][:12]:
            print("  %-30s %d" % (b, c))
        print("\n长尾词建议：")
        for x in kw["long_tail"]:
            print("  - " + x)
        return

    kw = mine_keywords(a.market)
    keys = [x.strip() for x in a.lang.split(",") if x.strip()]
    api_key = None
    if a.engine == "ai":
        if a.ai_key_file and os.path.exists(a.ai_key_file):
            api_key = open(a.ai_key_file, encoding="utf-8").read().strip()
        else:
            api_key = os.environ.get("AI_API_KEY")
        if not api_key:
            print("[!] 未拿到 API key（--ai-key-file 或环境变量 AI_API_KEY），回落到离线模板引擎")
    out = []
    for lang in keys:
        if a.engine == "ai" and api_key:
            try:
                out.append(gen_ai(a.product, lang, a.platform, kw, api_key, a.ai_model, a.brand))
                print("[+] %s  AI 生成完成" % LANG_NAME.get(lang, lang))
                continue
            except Exception as e:
                print("[!] %s AI 失败（%s），回落模板" % (LANG_NAME.get(lang, lang), str(e)[:60]))
        out.append(gen_template(a.product, lang, a.platform, kw, a.brand))
        print("[+] %s  模板生成完成" % LANG_NAME.get(lang, lang))
    d = {"product": a.product, "platform": a.platform,
         "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
         "keywords": kw, "listings": out}
    json.dump(d, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n=== 生成结果（%s / %s）===" % (a.product, a.platform))
    for L in out:
        print("\n[%s] %s" % (L["lang_name"], L["title"]))
        for i, b in enumerate(L["bullets"], 1):
            print("   %d) %s" % (i, b[:100]))
    print("\n[+] 已写 %s" % a.out)


if __name__ == "__main__":
    main()
