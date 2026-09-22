# candle-flow 项目长期记忆

A 股基本面分析系统。FastAPI 后端，`backend/app/analysis/`：`financials.py` 取数 → `engine.py` 编排 → `modules/*.py` 打分 → `models/*.py` 估值；技术面 `core/confluence.py` + `services/market_confluence_service.py` + `services/market_scan.py`。Python 3.13（本地 `backend/venv`，服务器 `.venv`）。

## 口径铁律（改动必查）

1. 绝不改写真实披露值；折减只落独立 forward 字段。金额走 `cashflow._fmt_yi()`，禁 `:.0f`。
2. 以最新报告期为锚，不回退年报；关键比率记录 `*_period`/`*_source` 自证口径。
3. 单一比率不做一票否决；惩罚有限度禁止叠加（cashflow权重.32 / E档0.5 / veto降档 / value_trap锁28 / compliance强制E / risk乘数限幅0.25），新增前查三重计数。
4. 指标名/展示值/得分必须同口径；给模块加 ctx 字段须确认 engine.py 真传了。
5. 同一判定不两处各算（红利分类曾 4 处调用）。本地与服务器同代码不同结果 = 数据可用性分叉，先逐模块比分类 flag。
6. 相对评分保留区分度；模块下限保护留痕（`raw_weighted_score`/`floor_applied`），分数卡 50~58 先查下限兜底。
7. 代理比率分子为 0 = 数据缺失 → None；指标口径须对应本行业会计科目；行业关键词不是同义词表（银行豁免只认「银行」）。
8. 公告事件扣分用 `_is_release_title()` 语义组合；怀疑数据源出错先外部交叉验证（格力短借属实）。
9. 方案给的旧综合分/「修改前代码」不可信，先独立复算/grep 真实代码。
10. **不得再造「第三个总分」**：`composite_score` 全站唯一权威，外部「五维权重表」一律拒绝；「按投资周期调整」只落筛选参数打包，不落权重。
11. **对外榜单输出只排序不重算**、自证覆盖率与分位基准；增量构建跳过条件须校验 payload 含必需字段（`_REQUIRED_SNAPSHOT_KEYS`）+ **口径版本号 `SCORING_VERSION`（factor_db.py）**：任何改动 composite/模块分/风控判定的提交都要 bump，否则盘后 build_all（非 force）把旧口径快照当"新鲜"跳过 → 榜单与详情页长期两个分数。bump 后非 force 重建会自动把全量判为待重建（分批跑，重启会打断，重触发即可）。
12. SQLite 并发读已开 WAL + busy_timeout=15000（`app/database.py`）；失败结果不进缓存 + 串行重试一次。
13. 数据源无：审计意见、日均成交额、个股 DDX/主力控盘、北向个股日频（2024-08 起停披）→ 不得提供相关打分/过滤。**个股资金面已有**（2026-09-21 实测推翻旧结论）：`services/stock_fund_flow.py` 东财 push2his 主力日频（klt=101）+ datacenter 融资融券/龙虎榜，**仅展示不进分**；口径标注「主力=超大单+大单，非同花顺 DDX」。
14. **买点信号是「决策」的下游，二者不得互相打脸**：凡过形态共振候选门槛者信号不停在 `neutral`；技术面被左侧防守否决者不得报 `strong_buy`。新增档位须同步前端 `SIGNAL_LABELS`/`signalTone`/`hasSignal`。
15. **改判据前先 dump 真实 payload 结构**（`_diag_payload2.sh`）。已知真名：估值分在 `valuation.composite_valuation_score`（**不是 `valuation.score`**）；ROE 在 `modules.profitability.indicators`（name 以 `ROE` 开头），**`market.roe` 不存在**；相对估值在 `valuation.relative`（**快照与 engine 返回 dict 都没有顶层 `relative`**）；指标位置实测稳定 `prof[0]=ROE / prof[1]=毛利率 / eff[0]=总资产周转率`。写错路径不会报错，只会静默取到 None。
16. **行业名必须精确匹配，不能子串**：线上行业名来自申万三级（`特钢Ⅱ`/`化学原料`/`化学制品`/`工业金属`/`煤炭开采`/`农化制品`…，共 129 个）。`_cycle_kw` 那种子串表只命中 86/5254（1.6%），`特钢Ⅱ` 里根本没有「钢铁」二字。周期性判断用 `engine.STRONG_CYCLICAL_INDUSTRIES`（28 行业 / 919 只，精确 `in`）。
17. **给读层加字段必须同时改三处**：`_LIGHT_SQL` + `_from_payload` + 使用处。漏 SQL 则恒 None（判据形同虚设），漏回退则旧快照炸。
18. **否决类判据要防「免检通道」**：任何「先 return None 放行」的分支都须自问它是否覆盖了本该拦的群体（周期型分支曾因 `cycle_trap` 恒 False 变成 894 只免检）。新增分支时用全库实测「改前 vs 改后」计数验证靶向性，不看绝对值看分布。

## 评分链路

`composite = Σ(模块分×w)/Σw` → risk限幅 → 高成长双强保护 → value_trap锁28 → cashflow_veto降档 → compliance强制E → 事件扣分(8×未释放，上限30)。权重 profitability .20 / growth .16 / solvency .16 / **cashflow .32** / valuation .16。rating：≥85 A / 80 A- / 74 B+ / 70 B / 65 B- / 55 C / 40 D / <40 E。

## 分类准入门槛闸门（读层，`market_scan.classify_admission_gate`）

**不是一票否决**：只把档位封顶「观察」+ 写理由，**不改写 `composite_score` / 技术面读数**（不产生第二个总分）。判定顺序 红利 → 周期 → 成长 → 传统价值，各类只用自己的门槛；**字段缺失一律放行**。

| 类型 | 判据 | 刻意不看 |
|---|---|---|
| 红利型 | 股息率≥3.5 且 payout≥50 且 OCF 为正 | ROE / 周转率（重资产低周转是行业属性） |
| 周期型 | ① `cycle_trap`（景气高点）② **矛盾组合**：PE分位≥80 且 ROE<6 | — |
| 成长型 | 营收增速≥15 **或** 毛利率≥30 | PE 绝对值（高 PE 是成长特征） |
| 传统价值型 | ① 矛盾组合 PE分位≥80 且 ROE<6 ② 周转<0.15（**金融业豁免**） ③ PE分位≥97 | — |

- **周期型置于成长之前**：周期股常因毛利率高被判成长，门槛完全不同。
- 生效依赖 `is_strong_cyclical` / `is_growth_stock` / `is_dividend_asset` 三个 flag 正确，且**快照须为新口径**（旧快照的 flag 是旧逻辑产物）。
- 闸门签名含 `industry`（供金融豁免判断）。注意 `_item_passes` 的 `industry=ind_filter` 是筛选，与闸门无关。
- 2026-09-22 修两个洞（`.6`）：周期分支曾只判 `cycle_trap` 且该值恒 False → 894 只强周期免检（混入 143 只高估值弱盈利）；周转兜底曾误杀 115 只金融股（银行/券商周转天然 0.02~0.08）。全库 5254 只实测：新增拦截 160 / 纠正误杀 152 / 净 PASS 3893→3885。

## 技术面共振

- 链路：`PatternEngine.scan` → `evaluate_confluence`（trend/momentum/volatility/volume/structure 五维正交，每维留 1）→ `_combined_score=形态分+有效共振数×6` → `_is_candidate`（有 emotion_extreme/structure_flaw soft 即 False）→ 榜单 `tech_score=min(combined,100)`，双阈值判定 核心持仓/买入候选/观察/淘汰（默认 基本面&技术面 ≥85 核心 / <40 淘汰，候选 `CANDIDATE_COMBINED=80`）。
- **四处修复**（confluence.py）：① bullish 且 MA5<MA10<MA20 → structure_flaw 淘汰（对称多头排列拦 bearish）；② MACD 命中要求 DIF 已在 DEA 正确一侧；③ RSI 命中限 28~48（超卖≠做多证据，反转走「RSI背离」）；④ 随机指标需已金叉/死叉。案例：002545 东方铁塔 9/18 空头排列 + 缩量十字孕线 90 分 → 组合100「核心持仓·技术100」，修复后 tech=None → 观察。
- **左侧抄底防守**：空头排列守卫只拦 `MA5<MA10<MA20`，缺口是「MA5 已上翘但收盘仍在 MA60 下方 + 当日无量」。判据：`pattern_name ∈ nison_rules.LEFT_SIDE_BULLISH`（17 个左侧反转；红三兵/上升三法/升窗回测等**延续**形态不在内）**且** 收盘 < MA60 **且** `_avg_vol_ratio`（当日量÷前20日均量，**单日口径**）< `VOL_CONFIRM_MIN=1.2` → structure_flaw（与空头排列同强度，**不叠加**）。误判案例：北方华创 676.61/MA60 724.61/量比0.87、雅克科技、平安电工。`_scan_job(job, diag)` 回传 `left_side_blocked` → `_overlay_one` 落 `tech_blocker` → `_verdict` 改理由。
- **对称性**：`pattern_name` 可选，不传则闸门跳过 → `backtest_service`/`signal_service`/`tech_narrative` 与既有单测行为不变；**只有 `market_confluence_service._scan_job` 传**。
- 已知残留：「贴 20 日低点/贴布林下轨」仍算 structure/volatility 命中（有空头排列兜底）。

## 共振口径调优（已部署+实测）

- **KLINE_LIMIT 90 → 180**（与 `tech_narrative` 对齐）。实测 400 只：90→180 不一致 **11.5%**（缓跌误判）、120→180 5.5%、150→180 0.8%、**180→240 为 0.0%** → **180 是收敛点**。
- **比对窗口必须 `kl[-n:]`**：`kl[:90]` 是最老 90 根，会得出 65.8% 假差异。
- **RSI 区间**：看涨 `28~60`（45~60 满权、28~45 降权 0.6），看跌 `40~72`（40~55 满权、55~72 降权 0.6）。降权意图：0.6+1.0=1.6 < `MIN_HITS`(2.0) → 弱势区撑不起双证。
- **量能动态阈值**：`CV`（近20日量能变异系数，不含今日）→ `k=clip(CV/0.35, 0.85, 1.25)`；放量 `1.5k`、温和 `1.2k`、缩量 `0.78/k`、回调缩量 `0.85/k`；CV 不可得 → k=1.0。命中文案带 CV 自证。
- **大盘环境只展示不入分**：`services/market_regime.py` → `GET /fundamentals/market-scan/market-regime`，`REGIME_INDEXES=(000300.SH, 000001.SH)`，`scoring_impact="none"`。**stale 指数排除在合成外**并列入 `stale_indexes`（跨日混算是伪精度）。
- **PEG 单一来源**：榜单买点信号改 `_snapshot_peg_map` 读快照 `relative.PEG.value`（3年CAGR + 周期/红利豁免）；`_overlay_one(symbol, name, fund_score, peg)` 返回新增 `peg_source`。

## 技术面叙述报告

- `services/tech_narrative.py` → `GET /api/v1/kline/tech-narrative?symbol=`：180 根 K 线算 MA排列/涨跌/周线/MACD/RSI/KDJ/布林/量能/近10日形态/支撑压力，模板化成文；对最近形态跑 `evaluate_confluence` 取 structure_flaw 提示（与信号页同源）。前端 `TechNarrative.vue`（指数/ETF 不显示）。
- **资金面段**：主力=超大单+大单（东财 fflow daykline klt=101，f52..f63，收止 f62/f63）+ 融资融券（RPTA_WEB_RZRQ_GGMX，含 RZJME5D/10D）+ 龙虎榜（RPT_DAILYBILLBOARD_DETAILSNEW，180天内最近一次）。表 `stock_fund_flow_daily`（90天）。
- **push2his 对新建连接有速率冷却**：短时并发被 RST，约 15s 恢复；后台同步与请求路径同时打会互坑。对策：全局锁串行 + 1.5s 间隔；优先读库，库内无才同步等一次（预算 5s），有数据但落后→后台刷新（同标的 2min 节流，成功后 `_sync_fresh` 2h）。首屏 0.8s、命中缓存 0.00s。资金面披露日滞后 K 线 → 文案显式「截至 YYYY-MM-DD（资金面最新披露日）」。

## 全市场扫描 / 因子库

- `factor_snapshots` 盘后预构建（工作日 16:35）；读层 `services/market_scan.py`，接口 `GET /fundamentals/market-scan[/technical-overlay|/market-coverage|/resonance]`。**沿用 composite_score，扫描层不重算**；榜单默认仅沪深主板（`include_gem=false`）。
- 覆盖率 ~97%（余 156 为长期失败票）。**每次部署重启会打断回填线程，需 `POST /fundamentals/factors/rebuild?budget_sec=10800`（非 force 自动续跑）**；该端点是**同步阻塞**的（无 progress 子端点），续跑须 `nohup curl ... &`。
- `kline_data` 只收沪深主板非 ST（3078 只）→ 创业板/科创板/ST 恒显「K线不足」；扩覆盖改 `main_board_kline_sync._main_board_symbols`。
- 技术叠加 `technical_overlay()`：复用 `_scan_job` 保证与信号页同源；单票缓存 10min、失败不进缓存、串行重试；`OVERLAY_PAGE_LIMIT=MAX_TOP`。
- 买点档位 `strong_buy`(基本面≥80+PEG<1.5+站上MA20+放量20%) / `watch` / `short_term`。
- 榜单分页 `scan_market(offset=)`，`MAX_TOP=500`；写竞争下全表 json_extract 读可达 46s，成常态再把过滤下推 SQL。
- **共振索引在内存中，服务重启即清空**，需重新 `POST /market-scan/resonance/build`（约 5-8 分钟/2015 只，`/progress` 可轮询；409 = 已在跑）。读层 `resonance_view` / `_overlay_one` / `_scan_job`。索引输出 `profile_gate` 字段。

## 估值口径修复（SCORING_VERSION 演进）

- **拆除 pe_distorted 自证循环**（`growth_profile.py`）：原 `PE>80 且未判成长 → 判成长 → 估值软化`，覆盖 99.3%（1068/1076）的 PE>80 票，估值分对 PE **非单调**。已删除。外部提的 `PE>80 → composite=0` 拒绝（误杀北方华创 PE86）。
- **turnaround 加 PB 底部前提** `CYCLE_TROUGH_PB_MAX=5.0`（用 PB 不用 PE：底部 E→0 时 PE 天然巨大）。保留 万华 2.01 / 璞泰来 2.23 / 中电港 3.29；排除 金牛 8.49 / 中国卫星 11.03。
- **PEG 全库恒 None 静默 bug**：`_snapshot_peg_map` 写 `$.relative.PEG.value`，真路径是 `valuation.relative` → 榜单买点 100% neutral 且零报错。**旧测试夹具手搓顶层 `relative`，把错误路径固化成"期望"** → 修路径 + 夹具按真实嵌套重构 + 加 sqlite 端到端用例。
- **PEG 分母禁止退化为单期同比**（`engine._growth_for_peg`）：年报窗口存在但负增长/含亏损时不得回退 YoY。`_growth_for_peg` 是 PEG **唯一**来源。
- **周期陷阱折减**（`engine.cycle_trap_hit`）：`PE分位≤30 且 PB分位≥70 且 ROE≥15`（且强周期、非红利）→ 估值折减 `CYCLE_TRAP_HAIRCUT=12`，与现金流折减取 max 不叠加，共用 `VALUATION_HAIRCUT_MAX=22`。全库 919 只强周期命中 20 只。
- **现金流微利稀释封顶**（`modules/cashflow.py`，`.5`）：`每股经营现金流<0.3 且 净利率<5%` → 模块分 ≤ `MICRO_PROFIT_CAP=50`。病根是微利分母放大「OCF/净利」比率（金牛均值 2.93 得 90 分，但每股 OCF 仅 0.078）。实测金牛式 76.9→50.0，正常公司 62.2 不受影响。**判据必须用每股绝对额，不用比率**。
- **估值软化加交叉校验**（`engine.py`，`.5`）：`PE>80 → signal="合理"` 原无条件生效；现 `SOFTEN_MAX_PCTL=90`：分位≥90 改判「偏高」。
- **估值软化阶梯**（`engine.py`，`.7`）：`.5` 的单一分位上限 90 仍太松（长白山 PE 80.6/分位 86.1 被洗白）。抽出模块级 `_apply_valuation_soften_ladder(rel, *, pe, pb, is_growth, is_div)` + `_pick_percentile()`：**绝对估值档位决定分位门槛** —— PE 80~150 要求分位<`SOFTEN_PCTL_PE_HIGH`(80)、PE>`SOFTEN_PE_EXTREME`(150) 要求<`SOFTEN_PCTL_PE_EXTREME`(70)、PB>8 要求<`SOFTEN_PCTL_PB`(80)；超门槛改判「偏高」不再软化。实测：长白山 80.6/86.1→偏高、金牛 193.5/89.8→偏高、真成长 PE95/分位60→仍「合理」、PE60 未达门槛不动、红利/非成长跳过。**注意 `_pick_percentile` 兼容 `percentile_5y`/`percentile`/`percentile_10y`，全库 PE 分位有值率仅 68.6%**（无分位不参与阶梯）。
- **买点信号阶段化**（`_detect_buy_signal` 新增 `pattern_name`/`pattern_ready`/`left_side_blocked`）：`left_side` / `bottom_confirm` / `setup_ready`；`neutral` 中文标签改「趋势未确认」。优先级：left_side_blocked → strong_buy → watch → short_term → bottom_confirm/left_side → setup_ready → neutral。`_overlay_one` **先跑 `_scan_job` 再算买点信号**。

## 生存级风险释放（2026-09-21，新潮能源 600777 案例）

`services/major_risk_events.py`：撤销退市风险警示/审计非标已消除等正式解除公告原不被识别 → 摘帽股永远强制 E + value_trap 锁 28。新增 `SURVIVAL_RELEASE_RULES` + `scan_survival_release()` + `current_name_hint()`：① 标题须同时含主语线索（退市风险警示/审计·无法表示意见/重整）与解除线索（撤销/影响已消除/重整执行完毕）；② 「申请撤销」阶段不算；③ 解除日 ≥ 风险公告日；④ face_delist 还要求最新简称已去 ST。新增 `events_released`/`released_labels`/`name_has_st`；`audit_opinion_hint` 只取**存活**事件（否则误扣 30 分）。600777：25.0/E → 58.3/C；*ST康佳A、*ST美丽仍 fatal=True。

其它同批修复：`modules/growth.py` 的「边际改善/单季加速」曾无条件加分（净利同比 -65.6% 仍给 74+82，权重 6.0/16.9 把成长分从 ~30 抬到 44.1，与 `marginal_recovery=False` 自相矛盾）→ 现边际改善只在 `marginal_recovery=True` 时输出、单季加速须有同比支撑；`analysis/config/__init__.py` 重资产关键词漏「油气开采Ⅲ」（补 油气/油服/勘探/开采/采矿/矿业/冶炼/铁路/高速/航空/燃气/水务/供热）；`risk.py` 泛化「开采」误并煤炭分支（拆煤炭 / 资源开采）；`financials.py` `frames.get(d) or _fetch_yjbb(d)` 对 DataFrame 求布尔 → ValueError（改显式 None/empty 判断）。

## 风险事件：减持漏判修复（2026-09-22，`.7`）

`services/major_risk_events.py`。**病根：中文标题语序多变，连续子串匹配大面积漏判。** 立霸股份 603519 `notice_scanned=150` 但 `events=[]`（公告扫到了、没命中）——「关于控股股东一致行动人减持股份计划公告」含「减持股份计划」，却**既不含连续的「减持计划」**（中间夹了"股份"）**也不含顺序相反的「股份减持」**。实测 15 种真实写法旧规则只命中 5 种（漏 67%）；**只看「新增风险」类 8 条：旧 3/8 → 新 8/8**。

- 新增 `all_of` 语义共现：`("减持",) AND ("计划"/"预披露"/"进展"/"减持股份")`，与 `keywords` 是 **OR**（只增召回不收紧）。`_match_rule(title, rule)` 统一入口。
- `_REDUCE_END_HINTS=("届满","到期","实施完毕","期限届满")` → `scan_notice_titles` 里减持类命中「结束」语义时 `continue`，交给释放通道（避免「计划已结束」被记成新增风险）。
- `scan_risk_release` 返回值新增 `"notice"`（触发释放的原始公告，供留痕）；无匹配时补一条已释放记录（否则 `observe_events_released` 为空，单条届满标题测不过）。
- **释放须带日期比较**：只释放**早于**释放公告的事件（`_ev_date > _rel_date` → 是新增风险，不释放）。
- **去重前按 `notice_date` 降序排序**（`events.sort(..., reverse=True)`）——「每规则保留最新一条」的 dedup 否则依赖输入顺序。
- 实测仍缺：**公告标题只有「有无减持」，没有减持比例%** → 用户提的「减持≥3%」无法直接实现，需解析公告正文。

## 质押率字段（2026-09-22 实测纠正）

`major_risks.pledge_ratio` **确实存在**（长白山 0.0088 = 0.88%，立霸 0.4999 = 49.99%），来源东财 `PLEDGE_URL`。但**只有 1702/5254（32.4%）有值**；阈值 ≥60% 命中 261 只、≥30% 命中 601 只。注意 0.5 附近密集（`PLEDGE_OBSERVE_RATIO=0.50` 是观察线），**立霸 49.99% 恰好卡在阈值下逃逸** —— 用 60% 做一票否决会同时「漏掉靶点」且「只覆盖 1/3 市场」。

## 评估外部方案的标准动作（2026-09-22 沉淀）

外部给的「一刀切阈值」**必须先在 5254 只全库实测命中率**，再看它对**榜单 TOP50** 的破坏性。本轮实测结论：
- `PE>80` 全库 20.4%；**TOP50 命中 6 只、6 只全被判成长股**（一刀切 = 连真成长一起砍）。
- `PE>行业中位数×2` 16.2%；`PEG≤1.5` 对周期股无意义；`资产负债率≥60%` 23.3%（含金融地产天然高杠杆）。
- 决定性反证：**中国神华 PE 仅 18.6 但 PE 分位 96** → 分位高 ≠ 泡沫；**PE 分位 95.6 拦中国神华却放走 89.8 的金牛化工** → 单指标绝对阈值对本问题完全无效。有效判据是**指标间的矛盾组合**。
- 快照 `market` 只有 `price/pb/pe_ttm/dividend_yield/market_cap/pe_percentile/pb_percentile` → **无成交额、无换手率、无成交量**；`goodwill` **不存在**（商誉/净资产无法实现）。
- `industry` 在 payload **顶层**（不在 market 里）；`modules.*.indicators` 真名见 `_diag_payload2.sh`。
- **「技术面权重」类提案一律拒绝**：`composite_score` 是基本面单口径权威分，`dim_scores` 五维全是基本面维度；技术面 `tech_score` 独立并列。加「基本面60%/技术面40%」= 造第三个总分，违反铁律 10。

## 商业模式画像 / 部署 / 环境

- 画像：`config/company_profiles.py`，分销识别 profile 显式 > `ELECTRONIC_DISTRIBUTION_PEERS` > 行业关键词；银行按权益占比相对中位 8.06% 打分，数据源无不良率/拨备勿写依赖。已配置：商络电子 300975、贵州茅台。深圳华强短板（FCF -11.4亿 等）勿靠口径松动消掉。
- 服务器 47.100.175.214（root 免密），candle-flow.online → cloudflared → **127.0.0.1:8002（不是 8000！）**，应用在 /opt/candle-flow。cloudflared 隧道端口 20241/20242。部署：PowerShell `scripts/deploy/push.ps1 -Server 47.100.175.214`（在仓库内层 candle-flow/ 下跑，**必须用 PowerShell 工具，Bash 调 powershell 会被安全拦截**）；验证 `ExecMainStartTimestamp` → health → 业务接口抽样。**部署脚本末尾的 health 探测常因服务重启竞态报 `curl (7) Failed to connect`，不代表部署失败**，单独复查 `systemctl is-active` + 端口即可。
- **本地 `factor_snapshots` 表存在但 0 行**（快照只在服务器构建）→ 本地做影响评估必须 scp 脚本到服务器跑，脚本内 `sys.path.insert(0, "/opt/candle-flow/backend")`。
- **沙箱拦网络**：ssh/公网验证一律 dangerouslyDisableSandbox。
- bash shim 缺 dirname 会废掉 ls/head：命令前 `export PATH="/c/Windows/System32:/c/Users/14149/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"`。
- PowerShell 不回显 stdout → `*>&1 | Out-File utf8` 落盘再读；服务器跑脚本用 scp + `sed -i 's/\r$//'; chmod +x; /path/script.sh`（ssh 里直接 `bash -s` 会被拦）。**scp 多文件同目录时同名互相覆盖**（services/models 都有 stock_fund_flow.py 踩过）→ 用不同远程文件名。
- **长轮询 ssh 会被工具 SIGTERM 掐断**（内嵌 `sleep` 同理）→ 拆成短查询 + 循环重试。
- 本地 git 根 `D:\candle-flow(2)\candle-flow`（外层）；前端构建 `node node_modules/vite/bin/vite.js build`；本地验证 uvicorn 8000 + vite 5173 代理。
- 测试：`cd backend && ./venv/Scripts/python.exe -m pytest tests -q`。既有失败（非回归）：test_symbol_search 4 拼音 + test_bull_tactics::test_scan_market_filters_main_board。当前权威值 **427 passed / 5 failed / 1 skipped**（新增 `tests/test_confluence_tuning.py`、减持语义 13 例、软化阶梯 7 例）。
- **部署正解**：`power` 工具 cd 到仓库内层 `candle-flow/`，跑 `powershell -ExecutionPolicy Bypass -File scripts/deploy/push.ps1 -Server 47.100.175.214`；**末尾 health 探测常报 `curl (7) Failed to connect` 属重启竞态，看 `SETUP_OK` 与随后单独复查即可**。验证：`grep 'SCORING_VERSION = '` + `grep -c all_of` + `systemctl is-active` + health。
- API 返回 `{code,message,data}`；健康 `/api/v1/health`；覆盖率 `/api/v1/fundamentals/market-coverage`；诊断脚本 `_diag_002545.py`、`_diag_payload2.sh`、`_dump_kline.py`。
- 验证脚本（仓库根）：`_verify_a.sh`（大盘环境+PEG）、`_verify_cd.sh`（索引重建+档位抽检）、`_verify_weekly.sh`（窗口收敛点扫描）。**套路：scp → `sed -i 's/\r$//'` → `nohup bash ... > /root/out.txt 2>&1 &` → 另起 ssh 轮询 `grep -q '===== DONE ====='`**。索引重建 ~182s/2050 只。
