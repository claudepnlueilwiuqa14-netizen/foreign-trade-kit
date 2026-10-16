#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
inquiry.py —— 外贸询盘自动跟进（读 crm.py 的 crm_data.json，零依赖）

干什么：
  due     列出「今日该跟进 / 已逾期」的询盘，按 层(A/B/C/D) × 逾期天数 × 金额 排优先级
  draft   给指定询盘生成一封英文跟进邮件草稿（按 阶段×分层 选话术），落 outbox/*.md
  sweep   批量给所有逾期询盘生成草稿 + 打印今日跟进队列
  log     记录一次跟进（直接复用 crm.follow，可设下次跟进天数）

设计原则（本机实测踩过的坑）：
  1) 纯本地模板，不依赖 LLM —— 半夜跑也不会因为模型挂了交不出东西；
     要 LLM 润色自己把 outbox/*.md 喂给网关（8787）即可。
  2) 话术按 stage 分六类：新询盘/已报价/谈判中/已打样/已下单/已流失(唤醒)。
  3) 语气按 tier 分四档：A 顾问式长文、B 标准、C 短模板、D 季度唤醒。
  4) 每个草稿文件带头部元信息（优先级/逾期天数/建议动作），中文可读。

用法：
  python inquiry.py due
  python inquiry.py draft --id 3
  python inquiry.py sweep --days 7
  python inquiry.py log --id 3 --note "客户回复问 MOQ" --next 2
"""
import os
import sys
import json
import time
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import load, follow, STAGES  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(HERE, "outbox")

TIER_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}

# 每个阶段的话术骨架（英文正文，{...} 占位符在 render() 里填）
BODY = {
    "新询盘": [
        "Hi {first}, thank you for your inquiry about {product}.",
        "We have reviewed your requirement for {qty} pcs and can confirm we are able to supply this item with stable quality and on-time delivery.",
        "To move forward quickly, could you confirm the following:",
        "  1) Target delivery time and destination port",
        "  2) Any certification requirements (CE / FCC / RoHS)",
        "  3) Preferred payment terms for the first order",
        "Once confirmed, we will send a formal quotation within 24 hours.",
        "Best regards,",
    ],
    "已报价": [
        "Hi {first}, following up on the quotation we sent for {product} ({qty} pcs).",
        "The quoted price of {price} USD/pc is valid until {expires}, and it already includes {incoterm} terms.",
        "If the budget is a concern, we have two options:",
        "  Option A: adjust the specification slightly (keeps the same look, lower cost)",
        "  Option B: place a trial order at a smaller quantity to test the market first",
        "Would either of these work for you? Happy to walk through the numbers on a quick call.",
        "Best regards,",
    ],
    "谈判中": [
        "Hi {first}, thanks for the detailed feedback on {product}.",
        "We have internally reviewed your target price. Here is what we can do:",
        "  - Hold the current price if quantity can be increased to {qty} pcs",
        "  - Or offer a step-price schedule: lower unit price as cumulative volume grows",
        "Our factory has confirmed the production slot for this month, so locking the quantity now protects your delivery date.",
        "Can we proceed on this basis?",
        "Best regards,",
    ],
    "已打样": [
        "Hi {first}, the pre-production sample for {product} is ready.",
        "We will send you photos and a short video before shipping the physical sample, so you can approve remotely and save courier time.",
        "After sample approval, the lead time for {qty} pcs is approximately {leadtime} days.",
        "Please confirm the shipping address for the sample.",
        "Best regards,",
    ],
    "已下单": [
        "Hi {first}, quick update on your order for {product}.",
        "Production is on schedule and we will share the inspection report before shipment.",
        "If you need anything adjusted for the next order, now is the ideal time to plan it together with this one.",
        "Thank you again for your trust.",
        "Best regards,",
    ],
    "已流失": [
        "Hi {first}, it has been a while since our last conversation about {product}.",
        "We have since updated the production line and the current cost structure is noticeably better than last year.",
        "If the project is still active, we can send a refreshed quotation at no obligation. If the timing is wrong, just reply \"later\" and we will check back next quarter.",
        "Either way, thank you for the time you gave us.",
        "Best regards,",
    ],
}

TIER_STYLE = {
    "A": "顾问式：可加一句定制化观察（客户市场/竞品），结尾给两个明确选项",
    "B": "标准：事实 + 选项 + 明确 CTA",
    "C": "短模板：3 句以内，只推一个动作",
    "D": "唤醒：不给压力，允许\"later\"，保持关系",
}


def days_since(datestr):
    if not datestr:
        return 999
    try:
        d0 = datetime.datetime.strptime(datestr[:10], "%Y-%m-%d").date()
    except ValueError:
        return 999
    return (datetime.date.today() - d0).days


def due_leads(d=None):
    """今日到期或已逾期、且未流失的询盘。"""
    d = d or load()
    today = datetime.date.today().isoformat()
    out = []
    for l in d["leads"]:
        if l.get("stage") == "已流失":
            continue
        nf = l.get("next_follow")
        if nf and nf <= today:
            out.append(l)
    return out


def priority(l):
    """优先级分：层权重 × 逾期天数 × 金额（越大越靠前）。"""
    tier_w = {"A": 100, "B": 60, "C": 25, "D": 10}.get(l.get("tier", "D"), 10)
    od = max(days_since(l.get("next_follow")), 0)
    amt = min((l.get("qty") or 0) * (l.get("target_price") or 0) / 1000.0, 50)
    return round(tier_w * (1 + od / 7.0) + amt, 1)


def queue_rows(leads):
    rows = []
    for l in leads:
        rows.append({
            "id": l["id"], "name": l.get("name", ""), "tier": l.get("tier", "D"),
            "stage": l.get("stage", "新询盘"), "qty": l.get("qty"),
            "target": l.get("target_price"), "email": l.get("email", ""),
            "overdue_days": max(days_since(l.get("next_follow")), 0),
            "last_contact": l.get("last_contact", "")[:10],
            "priority": priority(l),
        })
    rows.sort(key=lambda r: -r["priority"])
    return rows


def suggest_action(l):
    st = l.get("stage", "新询盘")
    od = max(days_since(l.get("next_follow")), 0)
    if st == "新询盘" and not l.get("quote_price"):
        return "首轮跟进：确认需求细节 + 承诺 24h 内出正式报价"
    if st == "已报价" and od >= 7:
        return "报价跟进超期：给 Option A/B 二选一，逼出决策"
    if st == "谈判中":
        return "谈判中：用数量/阶梯价换价格，别直接降价"
    if st == "已打样":
        return "样品阶段：先发照片视频确认，再寄样，省快递时间"
    if st == "已下单":
        return "已下单：报生产进度 + 铺垫下一单"
    return "按阶段话术跟进"


def render(l):
    """按 stage × tier 渲染英文邮件正文。"""
    st = l.get("stage", "新询盘")
    tpl = BODY.get(st, BODY["新询盘"])
    price = l.get("quote_price") or l.get("target_price") or 0
    exp = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    ctx = {
        "first": (l.get("name") or "there").split()[0],
        "product": l.get("product", "the item"),
        "qty": l.get("qty") or 0,
        "price": "%.2f" % price if price else "TBD",
        "expires": exp,
        "incoterm": "FOB",
        "leadtime": "25-30",
    }
    lines = [t.format(**ctx) for t in tpl]
    if l.get("tier") == "A":
        lines.insert(1, "I have checked your market pricing for this category and our offer stays competitive on total landed cost, not just unit price.")
    if l.get("tier") == "C":
        lines = lines[:3] + ["Just reply YES and we will send everything today."]
    if l.get("tier") == "D":
        lines = [lines[0], lines[1], lines[-2], lines[-1]]
    return lines, ctx


def draft(id_, tone="formal", out_dir=OUTBOX):
    d = load()
    l = next((x for x in d["leads"] if x["id"] == id_), None)
    if not l:
        return None
    body, ctx = render(l)
    today = datetime.date.today().isoformat()
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    fp = os.path.join(out_dir, "followup_%03d_%s.md" % (id_, today))

    greeting = "Dear" if tone == "formal" else "Hi"
    md = []
    md.append("# 跟进邮件草稿 · #%d %s" % (id_, l.get("name", "")))
    md.append("")
    md.append("| 项 | 值 |")
    md.append("|---|---|")
    md.append("| 询盘 ID | #%d |" % id_)
    md.append("| 客户 | %s %s |" % (l.get("name", ""), ("<" + l["email"] + ">") if l.get("email") else ""))
    md.append("| 国家 / 来源 | %s / %s |" % (l.get("country", ""), l.get("source", "")))
    md.append("| 阶段 / 分层 | %s / **%s** |" % (l.get("stage", ""), l.get("tier", "D")))
    md.append("| 优先级分 | %.1f |" % priority(l))
    md.append("| 逾期天数 | %d |" % max(days_since(l.get("next_follow")), 0))
    md.append("| 建议动作 | %s |" % suggest_action(l))
    md.append("| 语气说明 | %s |" % TIER_STYLE.get(l.get("tier", "D"), ""))
    md.append("")
    md.append("---")
    md.append("")
    md.append("**Subject:** Follow-up: %s — %s pcs" % (l.get("product", ""), l.get("qty") or ""))
    md.append("")
    md.append("**To:** %s" % (l.get("email") or "（未登记邮箱，从平台站内信发）"))
    md.append("")
    # 正文模板自带称呼（Hi {first}, ...），按 tone 换前缀，不重复也不丢上下文
    if body and body[0].startswith("Hi "):
        body[0] = greeting + " " + body[0][3:]
    md.append(body[0])
    md.append("")
    for ln in body[1:]:
        md.append(ln)
    md.append("")
    md.append("Zachary")
    md.append("")
    md.append("---")
    md.append("")
    md.append("<!-- 发送后记录：python inquiry.py log --id %d --note \"已发跟进邮件\" --next 3 -->" % id_)
    open(fp, "w", encoding="utf-8").write("\n".join(md))
    return {"file": fp, "subject": "Follow-up: %s — %s pcs" % (l.get("product", ""), l.get("qty") or ""),
            "lines": len(body), "tier": l.get("tier", "D"), "stage": l.get("stage", "")}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("due")
    dr = sub.add_parser("draft")
    dr.add_argument("--id", type=int, required=True)
    dr.add_argument("--tone", default="formal", choices=["formal", "friendly"])
    sw = sub.add_parser("sweep")
    sw.add_argument("--days", type=int, default=7)
    lg = sub.add_parser("log")
    lg.add_argument("--id", type=int, required=True)
    lg.add_argument("--note", required=True)
    lg.add_argument("--next", type=int, default=None)
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help(); return

    if a.cmd == "due":
        rows = queue_rows(due_leads())
        if not rows:
            print("[✓] 今日无待跟进询盘")
            return
        print("%-4s %-16s %-4s %-8s %8s %6s %8s" % ("ID", "客户", "层", "阶段", "金额", "逾期", "优先级"))
        print("-" * 68)
        for r in rows:
            print("%-4d %-16s %-4s %-8s %8.0f %5dd %8.1f" % (
                r["id"], r["name"][:16], r["tier"], r["stage"],
                (r["qty"] or 0) * (r["target"] or 0), r["overdue_days"], r["priority"]))
        print("-" * 68)
        print("共 %d 条待跟进。生成草稿：python inquiry.py draft --id <ID>" % len(rows))

    elif a.cmd == "draft":
        r = draft(a.id, a.tone)
        if not r:
            print("[!] 未找到询盘 #%d" % a.id); return
        print("[+] 草稿已生成：%s" % r["file"])
        print("    Subject: %s" % r["subject"])
        print("    阶段 %s / 分层 %s / 正文 %d 行" % (r["stage"], r["tier"], r["lines"]))

    elif a.cmd == "sweep":
        leads = due_leads()
        rows = queue_rows(leads)
        if not rows:
            print("[✓] 今日无待跟进询盘"); return
        made = 0
        for r in rows:
            d = draft(r["id"])
            if d:
                made += 1
        print("[+] 已批量生成 %d 封跟进草稿 -> outbox/" % made)
        print("\n今日跟进队列（按优先级）：")
        for i, r in enumerate(rows, 1):
            print("  %d. #%-3d %-16s %s类 %-8s 逾期%dd ｜ %s" % (
                i, r["id"], r["name"][:16], r["tier"], r["stage"],
                r["overdue_days"], suggest_action(next(x for x in leads if x["id"] == r["id"]))))

    elif a.cmd == "log":
        r = follow(a.id, a.note, a.next)
        print("[+] 已记录跟进 #%d" % a.id if r else "[!] 未找到询盘 #%d" % a.id)


if __name__ == "__main__":
    main()
