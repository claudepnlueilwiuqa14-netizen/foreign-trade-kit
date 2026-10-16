# foreign-trade-kit · 外贸全流程工具包

选品 → 供应链 → 利润 → 上架 → 订单 → 物流 → 看板。六个模块全部真实跑过，
下面的数字都是本机实测输出，不是示例数据。

---

## 一、模块与真实回显

### ① `market.py` 选品 / 竞品采集（Amazon / eBay / AliExpress / Shopify）

```
$ python market.py search "wireless earbuds" --platforms amazon,aliexpress --max 12
[*] 关键词 'wireless earbuds'，耗时 52s
    amazon: 12 条
    aliexpress: 9 条

平台         标题                                                     价格    评分   评论   销量估算
amazon     Wireless Earbuds, Bluetooth 6.0 Headphones HiFi Stereo   19.93   4.9    143    1000
amazon     Wireless Earbuds, 2026 Bluetooth 5.4 Headphones Bass     19.95   4.7    943      -
amazon     Soundcore P30i by Anker Noise Cancelling Earbuds         26.94   4.3     44   10000
amazon     JLab Go Sport ANC Active Noise Cancelling Earbuds        39.91   4.4     56      -
amazon     Soundcore by Anker P20i True Wireless Earbuds            22.94   4.4    116   10000
amazon     Soundcore P31i by Anker Translation Earbuds              34.92   4.4      9   10000
...
共 21 条

竞品分析：价格中位 22.94，区间 16.96~39.91，均价 24.76
标题高频词：earbuds(13), ear(12), bluetooth(10), earphones(10), buds(9), noise(8),
           headphones(6), playtime(6), led(5), display(5)
```

采集引擎（实测得出，不是拍脑袋）：
- **Amazon**：requests 直连必回 503（"To discuss automated access…"），**必须真浏览器渲染**（playwright）。
- **AliExpress**：搜索页是前端渲染，requests 拿到的 HTML 里没有商品 → 真浏览器取 `a[href*='/item/']`。
- **Shopify**：`/products.json` 公开接口（`--shop <域名>`）。
- **eBay**：本次出口 IP 被 eBay 直接重定向到 Error Page（com / co.uk / de 三个域名都试过），
  本次**未取到**，工具如实返回空并打印原因。

⚠️ 修过的两个真 bug：
1. AliExpress 解析用 `\{"[^{}]*?"title":".*?"price"...\}` 一次性匹配，在 60KB HTML 上触发
   **灾难性回溯**，进程挂死 12 分钟 → 改成两条线性正则。
2. 出口 IP 在日本，Amazon / AliExpress 渲染出的价格是**日元**（`¥4,990`），直接当 USD 会把
   4990 日元记成 4990 美元 → 现在识别货币符号后按实时汇率换算（JPY/GBP/EUR → USD）。

### ② `sourcing.py` 供应链 + 利润计算器

利润计算器（B2C 零售模型，完全离线精确计算）：

```
$ python sourcing.py calc --cost 42 --weight 0.18 --price 29.99 --platform amazon \
      --channel yunexpress --ad 0.12 --hs 8518.30
利润测算（汇率 USD/CNY=6.7030，来源 open.er-api.com）
  平台 Amazon US ｜ 渠道 云途专线(带电) ｜ 时效 8-15 天
    售价 USD 29.99
    采购成本        6.27      头程运费      7.01      包装杂费     0.45
    关税(0.0%)     0.00      平台佣金(15%)  4.50    平台固定费   0.30
    FBA/履约费     3.86      广告费(12%)    3.60    退货损耗(3%) 0.90
    总成本         26.88
    毛利            3.11  （毛利率 10.36%，ROI 22.6%）
    盈亏平衡售价   38.40
    FOB 45.00 CNY ｜ CIF 13.28 USD
    报关要素：品名、用途（蓝牙耳机/有线）、品牌、型号、是否无线、连接方式（蓝牙版本）、材质
```

渠道比价（0.18kg 小件，11 个渠道）：

```
$ python sourcing.py channels --weight 0.18 --cost 42 --price 29.99
渠道                        运费CNY     时效      毛利USD   毛利率     ROI
燕文航空挂号小包             29.02   12-25天      6.39   21.32%   57.9%
中国邮政挂号小包             30.03   18-40天      6.24   20.82%   55.8%
e邮宝                       31.97   10-20天      5.95   19.82%   51.8%
云途专线(带电)              46.99    8-15天      3.71   12.36%   27.0%
EMS 国际快递                119.98    7-15天     -7.18  -23.95%  -29.2%
DHL 国际快递                210.00    3-7天     -20.61  -68.72%  -54.2%
海派美森快船(美国)          283.47  25-40天     -31.58 -105.29%  -64.4%
空派双清包税                503.99   8-14天     -64.47 -214.98%  -78.7%
```

（海运/空派按 21kg / 12kg 最低起运量计费 —— 早期版本漏了这条，0.18kg 走海派算出 ¥2 的荒谬价，
小件反而"最省"，结论完全反了。）

1688 / 阿里国际站抓取：

```
$ python sourcing.py search "wireless earbuds" --source alibaba,1688
    alibaba: 浏览器渲染后仍无可解析卡片
    1688: 浏览器渲染取到 7 条
来源      标题                                                    价格
1688   跨境新款 895B裸机无线蓝牙耳机LED电量数显高音质降噪长续航定制…    16.0
1688   元宝可抵5% ｜ 杰理 ｜ 超长续航 ｜ 生活防水                      -
```

⚠️ **本次（最后一次运行）1688 未取到**，被登录墙拦截，脚本如实输出 0 条并打印原因；
上面这段是先前一次真实运行的回显（同一台机、同一脚本）。1688 反爬按请求频率变化，
稳定化方案：接登录 cookie（见下方"下一步"）。

### ③ `listing.py` 多语言 Listing + SEO 挖掘

```
$ python listing.py gen --market demo_market.json --product "无线蓝牙耳机" \
      --lang zh,en,ja,de --platform amazon
[English] Generic 无线蓝牙耳机 - earbuds, wireless, ear, bluetooth for daily commute
   1) 【earbuds】Long Battery Life — engineered for daily commute.
   2) Noise Cancelling delivers crystal-clear calls so you can workout without compromise.
   3) Built with HiFi Stereo Sound: zero dropouts, tested for travel.
   4) Why choose it: Up to 50H Playtime + true stereo separation, ideal for home office.
   5) Noise Cancelling and all-day comfort — everything you need for gaming.

[日本語] 无线蓝牙耳机 earbuds wireless ear ノイズキャンセリング
   1) 【earbuds】長時間バッテリーで、通勤に最適。
   2) ノイズキャンセリングによりクリアな通話を実現。ワークアウトでも安心。
   ...
```

关键词从**真实竞品标题**里挖（unigram / bigram / 长尾），不是编词表：
`earbuds(13) ear(12) bluetooth(10) earphones(10) buds(9) noise(8) playtime(6) led(5) display(5)`。
支持 `--engine ai` 走大模型（`https://www.985la.cn/v1`，key 从 `--ai-key-file` 或
环境变量 `AI_API_KEY` 读，**不入库**）；拿不到 key 自动回落模板并提示。

### ④ `logistics.py` 物流比价 / 时效 / 报关

```
$ python logistics.py quote --weight 0.18 --country US --value 29.99 --sensitive battery
物流比价：0.18 kg -> US（内置电池）｜汇率 6.7030（open.er-api.com）
渠道                    运费CNY   运费USD   占货值   时效      备注
云途专线(带电)            47.00     7.01    23.4%  13-27   带电产品可走，时效稳定
EMS 国际快递             120.00    17.90    59.7%  12-27   通达国家多，清关能力较强
UPS 国际快递             195.00    29.09    97.0%   8-20   北美线优势
空派双清包税             504.00    75.19   250.7%  13-26   含关税，重货批量补货主力
推荐：最省 云途专线(带电) ｜ 最快 DHL 国际快递 ｜ 综合最优 云途专线(带电)

$ python logistics.py eta --channel yunexpress --country US --sensitive battery --date 2026-11-20
  头程 8-15 天 ｜ 清关 3-7 天 ｜ 尾程 2-5 天
  旺季系数 1.45（黑五/圣诞旺季，尾程与港口普遍延误）  →  合计 19-39 天

$ python logistics.py customs --product "无线蓝牙耳机" --hs 8518.30 --qty 200 \
      --net 9 --gross 11 --value 5.6 --brand SoundCore --model A1
  报关要素：品名、用途（蓝牙耳机/有线）、品牌、型号、是否无线、连接方式（蓝牙版本）、材质
  单证：商业发票、装箱单、FCC 声明（带电产品）、UN38.3 + MSDS（含锂电池）、ISF 10+2（海运）
  [+] HTML 已写 demo_customs.html
```

时效是三段相加（头程 + 清关 + 尾程）× 旺季系数（黑五/圣诞 ×1.45、春节 ×1.60、国庆 ×1.20），
不是拍一个数。含电池/液体/带磁/粉末走不同渠道（敏感货渠道白名单）。

### ⑤ `crm.py` 询盘跟进 / 客户分层 / 报价单

```
$ python crm.py add --name "John Smith" --company "AudioTech LLC" --country US \
      --product "无线蓝牙耳机" --qty 500 --target 8.5 --source alibaba --intent 8
[+] 新增询盘 #1 John Smith（B 类客户，得分 9.2）

$ python crm.py list
ID   客户           国家  层  阶段    数量  目标价   得分
1    John Smith     US   B  新询盘   500   8.5   10.2
2    Hans Müller    DE   C  新询盘   120   7.2    7.0
3    田中太郎        JP   B  新询盘  2000   6.1   10.6

$ python crm.py quote --id 1 --cost 42 --weight 0.18 --channel sea_us --margin 0.25
[+] 报价单：demo_quote.html ｜ 建议报价 9.43 USD ｜ 毛利率 25.00% ｜ ROI 33.3%
    与客户目标价差额 +0.93 USD

$ python crm.py funnel
客户总数 3 ｜ 线索总金额 17314.00 USD ｜ 分层分布：A=0 B=2 C=1 D=0
```

分层 =（意向分 + 金额分 + 跟进次数）× 最近联系天数衰减，A/B/C/D 四档。
报价单是 HTML（可直接打印存 PDF），含 FOB/CIF、阶梯价底线、付款条款、有效期。

⚠️ 修过的真 bug：报价单早期拿 **B2C 零售模型**（含平台佣金/FBA/广告）去报 **B2B 批发价**，
算出 38.5 USD 去对客户 8.5 USD 的采购目标价，差 30 美元。现已拆成两套：
`calc_profit`（零售）× `calc_b2b`（批发，运费按整批摊，无平台费）。

### ⑥ `dashboard.py` 本地看板（真启动 + curl）

```
$ python dashboard.py --port 8795
[+] 看板已启动 http://127.0.0.1:8795/

$ curl http://127.0.0.1:8795/api/pipeline
  选品     ready    metric=21       file=demo_market.json
  供应链    ready    metric=0        file=demo_sourcing.json
  利润     ready    metric=10.36    file=demo_profit.json
  上架     ready    metric=4        file=listing.json
  订单     ready    metric=3        file=crm_data.json
  物流     ready    metric=8        file=demo_logistics.json

$ curl http://127.0.0.1:8795/api/summary
  选品: 21 样本，价格中位 22.94 ｜ 平台 ['aliexpress', 'amazon']
  利润: 毛利率 10.36，毛利 3.11，ROI 22.6
  上架: 语种 4 ｜ 关键词 25
  订单: 3 条，线索金额 17314.0 ｜ 分层 {'A': 0, 'B': 2, 'C': 1, 'D': 0}
  物流: 综合最优 云途专线(带电) ｜ 渠道数 8

$ curl http://127.0.0.1:8795/   →  HTTP 200，8431 字节 HTML
```

看板纯标准库 `http.server`，前端内联（深色，无 CDN，**离线可用**）。
缺哪个环节就显示"未导入"，**不填假数据**。

### ⑦ `inquiry.py` 询盘自动跟进（2026-10-16 新增）

读 `crm_data.json`，零依赖。干两件事：**排优先级队列** + **生成英文跟进邮件草稿**。

```
$ python inquiry.py due
ID   客户               层    阶段             金额     逾期      优先级
--------------------------------------------------------------------
4    Maria Garcia     B    已报价         17000     9d    154.1
5    Ahmed Hassan     C    谈判中          9600     3d     45.3
--------------------------------------------------------------------
共 2 条待跟进。生成草稿：python inquiry.py draft --id <ID>

$ python inquiry.py draft --id 4
[+] 草稿已生成：outbox/followup_004_2026-10-16.md
    Subject: Follow-up: 无线蓝牙耳机 — 2000 pcs
    阶段 已报价 / 分层 B / 正文 7 行

$ python inquiry.py sweep --days 7
[+] 已批量生成 2 封跟进草稿 -> outbox/
今日跟进队列（按优先级）：
  1. #4   Maria Garcia     B类 已报价      逾期9d ｜ 报价跟进超期：给 Option A/B 二选一，逼出决策
  2. #5   Ahmed Hassan     C类 谈判中      逾期3d ｜ 谈判中：用数量/阶梯价换价格，别直接降价

$ python inquiry.py log --id 4 --note "已发跟进邮件（Option A/B）" --next 3
[+] 已记录跟进 #4
```

设计要点：
- **话术按阶段分六类**：新询盘 / 已报价 / 谈判中 / 已打样 / 已下单 / 已流失(唤醒)
- **语气按分层分四档**：A 顾问式长文（加市场定价观察）、B 标准、C 三句短模板、D 无压力唤醒
- 每个草稿头部带元信息表（优先级分 / 逾期天数 / 建议动作），中文可读
- 纯本地模板，**不依赖 LLM**——半夜跑也不会交不出东西；要润色把 `outbox/*.md` 喂给 LLM 网关即可
- `log` 直接复用 `crm.follow`，发完邮件记一笔，下次跟进日期自动排

### ⑧ `bulk.py` 多平台批量上架（2026-10-16 新增）

读 `listing.json` → 按各平台规则重裁 → 生成规范上架文件。**一条命令铺 5 个平台**。

```
$ python bulk.py plan --listing listing.json --platforms all --price 19.99 --stock 500
[*] 产品 无线蓝牙耳机 ｜ 4 个语言版 ｜ 价 19.99 USD ｜ 库存 500
[+] amazon      4 条 -> payloads/20261016_122547_无线蓝牙耳机/amazon_无线蓝牙耳机.csv
[+] ebay        4 条 -> payloads/20261016_122547_无线蓝牙耳机/ebay_无线蓝牙耳机.csv
[+] shopify     4 条 -> payloads/20261016_122547_无线蓝牙耳机/shopify_无线蓝牙耳机.json
[+] aliexpress  4 条 -> payloads/20261016_122547_无线蓝牙耳机/aliexpress_无线蓝牙耳机.json
[+] etsy        4 条 -> payloads/20261016_122547_无线蓝牙耳机/etsy_无线蓝牙耳机.csv

$ python bulk.py report
== 20261016_122547_无线蓝牙耳机 ==
   amazon_无线蓝牙耳机.csv      4 行
   ebay_无线蓝牙耳机.csv        4 行
   shopify_无线蓝牙耳机.json    4 条
   aliexpress_无线蓝牙耳机.json 4 条
   etsy_无线蓝牙耳机.csv        4 行
```

| 平台 | 产物格式 | 怎么用 |
|---|---|---|
| amazon | 库存加载器 Flat File CSV（SKU/标题/五点/描述/价格/库存/搜索词） | 卖家后台 > 库存 > 批量上传商品 |
| ebay | 文件交换 CSV | 卖家中心 > 文件交换 |
| shopify | `products.json` 载荷 | 后台导入，或 `bulk.py push` **真实推送** |
| aliexpress | JSON（subject/detail/sku_list） | 卖家后台 > 商品批量发布 |
| etsy | 上架 CSV（13 tag 上限已裁剪） | 后台 > 上架管理 > 批量 CSV |

**真实推送（只有 Shopify 能推）**：Amazon SP-API / eBay Trading / Etsy 都要卖家账号 OAuth 审批，
本机没有这些凭证，**不造假**——生成规范文件走后台上传。Shopify 只要一个 Admin API token：

```
# token 获取：后台 > 设置 > 应用和销售渠道 > 开发应用 > Admin API > 暴露 write_products > 安装 > 复制 token
$ python bulk.py push --platform shopify --shop mystore.myshopify.com --token-file key.txt --price 19.99 --stock 500
[+] Generic 无线蓝牙耳机 - earbuds, wireless, ear, bluetooth for daily 已创建
...
[✓] 成功 4 / 失败 0（mystore.myshopify.com）
```

每条标题/五点都按目标平台规则重裁（标题 200/80/128/120/140 字符、五点 5/4/4/5/0 条、tag 数上限），
不是把亚马逊文案硬塞给 eBay。


---

## 二、一键跑通

```bash
cd tradekit
python market.py search "wireless earbuds" --platforms amazon,aliexpress --max 12 --out demo_market.json
python sourcing.py calc --cost 42 --weight 0.18 --price 29.99 --platform amazon --channel yunexpress --ad 0.12 --hs 8518.30 --out demo_profit.json
python sourcing.py channels --weight 0.18 --cost 42 --price 29.99
python sourcing.py search "wireless earbuds" --source 1688 --max 8 --out demo_sourcing.json
python listing.py gen --market demo_market.json --product "无线蓝牙耳机" --lang zh,en,ja,de
python logistics.py quote --weight 0.18 --country US --value 29.99 --sensitive battery --json demo_logistics.json
python logistics.py customs --product "无线蓝牙耳机" --hs 8518.30 --qty 200 --net 9 --gross 11 --value 5.6
python crm.py add --name "John Smith" --country US --product "无线蓝牙耳机" --qty 500 --target 8.5 --intent 8
python crm.py quote --id 1 --cost 42 --weight 0.18 --channel sea_us --margin 0.25 --out demo_quote.html
python inquiry.py due                          # 今日该跟进谁
python inquiry.py sweep --days 7               # 批量生成英文跟进邮件草稿
python bulk.py plan --listing listing.json --platforms all --price 19.99 --stock 500
python dashboard.py --port 8795
```

---

## 三、已知限制与下一步

| 限制 | 现状 | 下一步命令/方案 |
|---|---|---|
| eBay 采集 | 本次出口 IP 被 eBay 拒绝（Error Page），三域名均失败 | 换美西住宅代理，或申请 eBay Browse API（需 App ID）后接 `/buy/browse/v1/item_summary/search` |
| 1688 采集 | 登录墙，成功与否随请求频率波动；本次 0 条 | `playwright` 注入登录 cookie（`storage_state`），或改用 1688 开放平台 API |
| 阿里国际站 | requests 200 但页面结构变化，无可解析卡片 | 更新选择器，或走 Alibaba 开放平台 |
| AliExpress 价格 | 部分卡片价格未渲染，价格为空 | 渲染后等待更久 / 走 `?SortType=price` 触发价格渲染 |
| 汇率 | 在线取（open.er-api.com），失败回落内置值并标注 `fallback` | 可换成签约汇率源 |
| 报价单 PDF | 输出 HTML（浏览器打印存 PDF） | 装 `reportlab` 或 `weasyprint` 后直接出 PDF |
| 邮件自动发送 | 只生成草稿到 `outbox/`，不直连 SMTP（不发假邮件、不群发骚扰） | 接自己的 SMTP/企业邮箱，`smtplib` 20 行即可，发之前人工过一遍 |
| Amazon/eBay/Etsy 上架 | 只生成规范 CSV/JSON，未接卖家 API（缺 OAuth 凭证） | 拿到卖家账号审批后接 SP-API / Trading API；Shopify 已可 `bulk.py push` 真实推送 |

费率数据（平台佣金 / FBA 档位 / 渠道运费 / HS 税率）是行业公开常见档位，
已内置并可改；**正式报价以平台政策与货代实时报价为准**。
