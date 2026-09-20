# candle-flow 项目长期记忆

A 股基本面分析系统。FastAPI 后端，`backend/app/analysis/`：`financials.py` 取数 → `engine.py` 编排 → `modules/*.py` 打分 → `models/*.py` 估值。Python 3.13（`backend/venv`）。

## 口径铁律（24 条，改动必查）

1. 绝不改写真实披露值；折减只落独立 forward 字段。
2. 金额单位走 `cashflow._fmt_yi()`，禁用 `:.0f`。
3. 以最新报告期为锚，不回退年报。
4. 关键比率记录 `*_period`/`*_source` 自证口径。
5. 单一比率不做一票否决。
6. 惩罚有限度禁止叠加：现有层=cashflow权重0.32 / E档0.5 / cashflow_veto降档 / value_trap锁28 / compliance强制E / 风险乘数限幅0.25；新增前先查三重计数。
7. 指标名/展示值/得分必须同口径。
8. 给模块加 ctx 依赖字段必须确认 engine.py 真的传了（历史漏过 symbol）。
9. 同一判定不两处各算。曾：`classify_growth_stock` 同股两判；红利分类 4 处调用，股息率回退必须在 ctx 构建前（海螺水泥事故：东财失败时盈利判非红利/估值判红利 → veto 误锁 28）。**本地与服务器同代码不同结果 = 数据可用性分叉，先逐模块比分类 flag。**
10. 毛利率按模式取阈值：分销 ((12,100),(8,12),(4,8))，制造 ((50,100),(30,50),(15,30))。
11. 低净利率高周转行业用同业相对基准 `DISTRIBUTION_BENCHMARKS`（达中位≈55分）；现金流质量例外仍绝对口径。
12. 相对评分保留区分度：行业内最差者仍判低分。
13. 模块下限保护留痕（`raw_weighted_score`/`floor_applied`）；分数卡 50~58 先查下限兜底再改指标。
14. 凡「定性措辞+扣分」的函数都要查 `is_distribution` 分支。
15. 措辞与事实一致（被引用的对照锚未必健康）。
16. 部分科目求和的代理比率分子为 0 = 数据缺失 → None，消费端把 `<=0` 当缺失。
17. 指标口径须对应本行业会计科目，错配双向（银行 3 误低 1 误高）。
18. 行业关键词不是同义词表：监管指标不同源不得并入（银行豁免只认「银行」），改后验证非目标行业零变化。
19. 公告事件扣分有时效性：释放用 `_is_release_title()` 语义组合（减持+届满/到期），枚举关键词追不上标题变体。
20. 怀疑数据源出错前先外部信源交叉验证（格力 797 亿短借属实）。
21. 方案给的旧综合分不可信（非等权 + 否决层 + 事件扣分），必须独立复算。
22. 比率分母含扣减项须设上限（宁德 ROIC：超额现金抵扣 ≤有息资本50%，另出 `roic_gross_capital_pct`）；同一指标单一来源（DCF WACC 委托 `_estimate_wacc_pct`）；DCF 不影响综合分。
23. 方案引用的「修改前代码」可能是旧版本，判定前先 grep 真实代码；同证据重复加分的死代码块应移除（已移除红利框架外「资源壁垒溢价/红利定价锚」）。
24. **对外「全市场/榜单」输出必须自证覆盖率与分位基准，且只排序不重算**——展示层不得另立第二套权重；先把数据层完备性查清，再谈算法优化。
25. **增量构建的跳过条件必须校验 payload 含必需字段**，不能只看「主键已存在」：`build_all(force=False)` 只查 symbol 是否已有快照，导致新增字段（如 `profit_yoy`）在存量行上永不回填，下游静默降级。
26. **不得再造「第三个总分」**：`composite_score` 已含五维加权 + 风险乘数 + E档惩罚 + 各种 veto + 事件扣分，是全站唯一权威。任何「基本面×a% + 技术面×b% + 资金面×c%」的合成公式都会造成同股两分（个股分析页 vs 榜单页），且三维异构尺度直接加权无依据。外部方案给的「五维权重表」（如 25/20/20/15/20）一律拒绝——现金流 2 倍权重（.32）是为短板否决刻意设计，降到 .20 等于放松惩罚。可复算反例：四维 70 + 现金流 40（E档×0.5=20）时，系统 54.0(D) vs 方案 60.0(C)，自动升一档。**「按投资周期调整」只能落在筛选参数打包（风格预设），不能落在权重上。**
27. **SQLite 并发读要开 WAL**：默认 `journal_mode=delete` + `busy_timeout=5s`，盘后因子回填持写锁时，读接口会成片超时（实测技术叠加 13/40「分析失败」）。已在 `app/database.py` 对 sqlite 设 `PRAGMA journal_mode=WAL` + `busy_timeout=15000`。配套：**失败结果不得写进缓存**（否则固化错误），并做一次串行重试，`stats.failed/retried` 自证。

## 评分链路（改评分前必读）

`composite = Σ(惩罚后模块分×w)/Σw` → risk限幅 → 高成长双强保护 → value_trap锁28 → cashflow_veto降档 → compliance强制E → 事件扣分(8×未释放观察事件，上限30)。
权重 profitability .20 / growth .16 / solvency .16 / **cashflow .32** / valuation .16（efficiency/industry 仅展示）。
常量：`RISK_THRESHOLD=60`、`E_GRADE_SCORE=40`、`E_GRADE_PENALTY=0.5`、`CASHFLOW_VETO_THRESHOLD=40`、`RISK_MAX_PENALTY=0.25`。
rating：≥85 A / 80 A- / 74 B+ / 70 B / 65 B- / 55 C / 40 D / <40 E。

## 全市场扫描 / 因子库（2026-09-20 新增）

- `factor_snapshots` 由 `factor_db.build_all()` 盘后预构建（挂在 `main_board_kline_sync._scheduled_sync`，工作日 16:35）。**长期只写不读**——2026-09-20 才补上读层 `services/market_scan.py`。
- 读层接口：`GET /api/v1/fundamentals/market-scan`（top/min_composite/min_market_cap_yi/exclude_st/industry/keyword/include_gem/sort_by）、`GET /market-coverage`。**沿用 `composite_score`，扫描层不重算**；分位（market_pct/industry_pct）仅展示、缺失不赋 50。**榜单默认仅沪深主板**（`include_gem=false` 剔除 300/301/302/688/689），名称/代码搜索走 `keyword`。
- **真实瓶颈是覆盖率，不是算法**：线上 1112/5382 = **20.7%**，日志 `factor build BATCH DEADLINE 1800s reached, 4274 remaining`；单只 ≈13s × 8 并发 → 全市场 ≈2.4h。非披露期只补缺失 → 多日渐进；一次性补齐用 `POST /fundamentals/factors/rebuild?budget_sec=&max_symbols=` 或 env `FACTOR_BUILD_DEADLINE_SEC`。
- **2026-09-20 已补齐到 5211/5382 = 96.8%**（`budget_sec=10800`，11:07→14:16，末尾 23 只未完成；`complete=false`）。
- **⚠️ profit_yoy 存量缺口（守卫已上线，回填中）**：5211 条存量快照缺 `profit_yoy` → overlay PEG 恒 None → **「强买入」档不可达**。已加 schema 守卫（`_REQUIRED_SNAPSHOT_KEYS` + `_outdated_snapshot_symbols`，按 `json_type(...) IS NULL` 判键不存在），2026-09-20 15:30 起非 force 重建自动回填，速率 ≈0.43 只/秒、全量约 3.5h；16:28 进度 1713/5212，且 `strong_buy` 已实测出现。**每次部署重启都会打断回填线程，需重新触发 `POST /rebuild?budget_sec=10800`（非 force，守卫自动续跑不重算）。** **给 `run_full_analysis` 加返回字段时，把键名追加进 `_REQUIRED_SNAPSHOT_KEYS` 即可自愈。**
- **K 线库覆盖范围（口径）**：`kline_data` 只收**沪深主板非 ST**（`is_main_board` 前缀 600/601/603/605 + 000/001/002/003，加 `is_st_name` 过滤），线上 3078 只 / 78.2 万根 / 最新交易日准时。因子快照 5211 只中 **2148 只无 K 线**（创业板 1392 + 科创板 611 + ST 197）→ overlay/形态页对创业板·科创板·ST 恒显「K线不足」；「最近形态」页同库，创业板/科创板永不出现。要扩覆盖需改 `main_board_kline_sync._main_board_symbols`（该库原为战法扫描而建，战法侧自身有过滤，扩表不影响它）。
- **技术叠加层** `technical_overlay()`：复用 `MarketConfluenceService._scan_job` 保证与信号页口径一致 + `_detect_buy_signal`，8 并发；**综合分不变**，只加技术字段。2026-09-20 起：**覆盖本页全部标的**（`OVERLAY_PAGE_LIMIT = MAX_TOP`，不再截断 120）+ **单票级缓存** `_overlay_item_cache`（10min，翻页只算新出现的票）+ 页面级缓存；`stats` 带 `analyzed/computed/retried/failed` 自证；失败票**不进缓存**并串行重试一次。`GET /fundamentals/market-scan/technical-overlay?top≤500&offset=`。成本 ≈0.3s/只。
- **技术面达标线已有等价物**：`market_confluence_service.CANDIDATE_COMBINED = 80.0`（共振组合分=形态分+有效共振数×6）；买点档位 `strong_buy`(基本面≥80+PEG<1.5+站上MA20+放量20%) / `watch`(≥80+PEG>2+回踩MA60缩量) / `short_term`(基本面<60+突破MA20放量)。技术面维度系统已含 trend/momentum/volatility/volume/structure（`core/confluence.py` 维度映射），**缺「大盘环境」**（个股共振不含大盘，勿擅自加入否则破坏与信号页同源）。
- 快照会滞后（实测 2 天）→ 榜单继承因子库新鲜度，靠 `coverage.stale_days` 自证。
- 数据源**无**「审计意见」「日均成交额」字段 → 不得提供这两项过滤（会全通过 = 假过滤）；`audit_opinion` 只是公告标题 hint，默认「标准无保留」。
- **数据源无个股级资金面**：全库无 `stock_individual_fund_flow` / 北向持仓 / 机构席位 / 龙虎榜取数。只有 `api/v1/flow.py` 的**宽基**分时与 `fundamental_screen._fetch_sector_fund_flow()` 的**板块**资金流排名（akshare `stock_sector_fund_flow_rank`，盘后口径）。→ **不得承诺「资金面」打分/过滤/权重**；需要资金面只能做板块级代理并显式声明口径差异。
- **榜单分页**：`scan_market(offset=)` → 返回 `offset/matched/has_more`；`top` 上限 `MAX_TOP=500`。`market.price` 已进 item（覆盖 5204/5212）。写竞争下全表 `json_extract` 读取可达 46s（缓存命中也要先付这次读）；常态应远快于此，若成常态再把 keyword/过滤下推 SQL。

## 商业模式画像（`config/company_profiles.py`）

- 入口：`get_company_profile` / `business_model_of` / `is_distribution` / `peers_for`。
- 分销识别优先级：profile 显式 > `ELECTRONIC_DISTRIBUTION_PEERS`（000062/001287/001298/300131/300184/300475/300493/301099）> 行业关键词；影响 cashflow/profitability/solvency/risk/comps。
- 银行：`business_model_of`→"bank"；偿债按权益占比相对中位 8.06%：`clamp(58+(x-8.06)*5, 35, 76)`，跳过流动/速动/有息；数据源**无**不良率/拨备/资本充足率，勿写依赖字段。
- 分数随行情/PE 漂移（同日 0.5~1.6 分），前后对比先确认代码路径真被触达。
- 深圳华强余下短板勿靠口径松动消掉：FCF -11.4亿、OCF/净利 -2.04、PE 39；cashflow 被 dist_expanding 下限保护（47.2→52.0）。
- 已配置画像：商络电子 300975（分销）、贵州茅台（白酒风险块）。

## 部署

- 服务器 47.100.175.214（root 免密）；candle-flow.online → cloudflared → 127.0.0.1:8002；systemd：candle-flow / candle-flow-daily.timer / cloudflared；应用目录 /opt/candle-flow。
- 部署：PowerShell 跑 `scripts/deploy/push.ps1 -Server 47.100.175.214`（勿用 bash）。`.env` 与 `data/` 不上传，新机需手工放 `.env`。
- 退出码 1+缺 SETUP_OK ≠ 失败（Tee-Object 捕获超长 stderr 中断）；可靠判据：服务器上 `~/candle-flow-deploy.tgz` 不存在 = 脚本跑完。
- 验证新代码已加载：`systemctl show candle-flow -p ExecMainStartTimestamp` 晚于源文件 mtime → curl health → 业务接口跨行业抽样。

## 环境

- **沙箱会拦网络（2026-09-20 起必查）**：sandboxed 下 `ssh` 报 `Connection timed out during banner exchange`、公网 https 返 530 = 沙箱假象（TCP 层 Test-NetConnection 反而 True），非服务器故障。部署/ssh/公网验证一律 `dangerouslyDisableSandbox=true`。
- **PowerShell 不回显 stdout** → `Out-File` 落盘（utf8）+ Read 工具读；内联 ssh 的引号/括号会炸 → 本地写 `.sh` 再 `Get-Content x.sh -Raw | ssh host "bash -s"`（CRLF 会报 `$'\r'`，无碍）。
- 本地 git 在 `D:\Program Files\Git\cmd\git.exe`；**生效仓库根是 `D:\candle-flow(2)\candle-flow`**（外层），`candle-flow/candle-flow` 内的 .git 无效。
- 前端构建：`node node_modules/vite/bin/vite.js build`（`npx vite build` 会误落 WSL shim 报「没有已安装的分发」）。
- bash 需注入 PortableGit PATH 才有 git/ssh。
- 前端本地验证：`venv/Scripts/uvicorn.exe app.main:app --port 8000` + `vite --port 5173`（代理 /api→8000）；agent-browser 已装（`C:\Users\14149\.npm-global\agent-browser.cmd`，Chrome 在 `~/.agent-browser`）——**daemon 不跨工具调用保留**，open/wait/select/screenshot/close 必须同一调用内 `&&` 链式执行；截图参数是 `--full`（无 --full-page）；select 用 option 的 value 而非 label；`click "button.btn-secondary"` 会点到第一个同 class 按钮 → 用 `find text "..." click`。
- 测试：`cd backend && ./venv/Scripts/python.exe -m pytest tests -q`。
- 既有失败（非回归）：`test_symbol_search.py` 4 个拼音用例 + `test_bull_tactics.py::test_scan_market_filters_main_board`（板块扫描，缺 pypinyin 资源）。注意后者名字含 scan_market 但与 `services/market_scan.py` 无关。
- 诊断脚本：`_diag_bank.py <symbol>`（模块明细）、`_diag_cycle.py`（周期/红利股估值明细）、`_diag_scan.py`（用线上快照验证全市场读层，需先取回 `_factor_dump.json`）。
- API 返回 `{code,message,data}`：评分在 `data.composite_score/final_rating`，模块在 `data.modules.<name>`。
