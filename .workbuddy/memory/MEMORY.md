# candle-flow 项目长期记忆

A 股基本面分析系统。后端 FastAPI，`backend/app/analysis/`：`financials.py` 取数 → `engine.py` 编排 → `modules/*.py` 打分 → `models/*.py` 估值。Python 3.13（`backend/venv`）。

## 口径铁律（23条，改动必查）

1. 绝不改写真实披露值；任何折减只落独立 forward 字段。
2. 金额单位走 `cashflow._fmt_yi()`，禁用 `:.0f`。
3. 以最新报告期为锚，不回退年报。
4. 关键比率记录 `*_period`/`*_source` 自证口径。
5. 单一比率不做一票否决。
6. 惩罚有限度禁止叠加：现有层=cashflow权重0.32 / E档0.5 / cashflow_veto降档 / value_trap锁28 / compliance强制E / 风险乘数限幅0.25；新增前先查三重计数。
7. 指标名/展示值/得分必须同口径。
8. 给模块加 ctx 依赖字段必须确认 engine.py 真的传了（历史漏过 symbol）。
9. 同一判定不两处各算（曾 classify_growth_stock 同股两判，PEG 静默失效）。
10. 毛利率按模式取阈值：分销((12,100),(8,12),(4,8))，制造((50,100),(30,50),(15,30))。
11. 低净利率高周转行业用同业相对基准 `DISTRIBUTION_BENCHMARKS`（达中位≈55分）；现金流质量例外仍绝对口径。
12. 相对评分保留区分度：行业内最差者仍判低分。
13. 模块下限保护留痕（raw_weighted_score/floor_applied）；分数卡 50~58 先查下限兜底再改指标。
14. 凡「定性措辞+扣分」的函数都要查 is_distribution 分支。
15. 措辞与事实一致（被引用的对照锚未必健康）。
16. 部分科目求和的代理比率分子为 0=数据缺失→None，消费端把 <=0 当缺失。
17. 指标口径须对应本行业会计科目，错配双向（银行 3 误低 1 误高）。
18. 行业关键词不是同义词表：监管指标不同源不得并入（银行豁免只认「银行」），改动后验证非目标行业零变化。
19. 公告事件扣分有时效性：释放用 `_is_release_title()` 语义组合（减持+届满/到期），枚举关键词追不上标题变体。
20. 怀疑数据源出错前先外部信源交叉验证（格力 797 亿短借属实）。
21. 方案给的旧综合分不可信：非等权重+否决层+事件扣分，必须独立复算。
22. 比率分母含扣减项须设上限（宁德 ROIC 事故：超额现金抵扣≤有息资本50%，另出 `roic_gross_capital_pct`）；同一指标单一来源（DCF WACC 委托 `_estimate_wacc_pct`）；DCF 不影响综合分。
23. 方案引用的「修改前代码」可能是旧版本，判定前先 grep 真实代码；同证据重复加分、关键词对实际行业名不生效的死代码溢价块应移除（2026-09-20 已移除红利框架外的「资源壁垒溢价/红利定价锚」，V型信号传递与 ctx 传参指控均为已实现）。

## 评分链路（改评分前必读）

`composite = Σ(惩罚后模块分×w)/Σw` → risk限幅 → 高成长双强保护 → value_trap锁28 → cashflow_veto降档 → compliance强制E → 事件扣分(8×未释放观察事件，上限30)。
权重 profitability .20 / growth .16 / solvency .16 / **cashflow .32** / valuation .16（efficiency/industry 仅展示）。
常量：`RISK_THRESHOLD=60`、`E_GRADE_SCORE=40`、`E_GRADE_PENALTY=0.5`、`CASHFLOW_VETO_THRESHOLD=40`、`RISK_MAX_PENALTY=0.25`。
rating：≥85 A / 80 A- / 74 B+ / 70 B / 65 B- / 55 C / 40 D / <40 E。

## 商业模式画像（`config/company_profiles.py`）

- 入口：`get_company_profile` / `business_model_of` / `is_distribution` / `peers_for`。
- 分销模式识别优先级：profile 显式 > `ELECTRONIC_DISTRIBUTION_PEERS` 名单（000062/001287/001298/300131/300184/300475/300493/301099）> 行业关键词；影响 cashflow/profitability/solvency/risk/comps。
- 银行：`business_model_of`→"bank"；偿债按权益占比相对中位 8.06% 给分 `clamp(58+(x-8.06)*5, 35, 76)`，跳过流动/速动/有息；数据源**无**不良率/拨备/资本充足率，勿写依赖字段。
- 分数随行情/PE 漂移（同日 0.5~1.6 分），前后对比先确认代码路径真被触达。
- 深圳华强余下短板勿靠口径松动消掉：FCF -11.4亿、OCF/净利 -2.04、PE 39；cashflow 被 dist_expanding 下限保护（47.2→52.0）。
- 已配置画像：商络电子 300975（分销）、贵州茅台（白酒风险块）。

## 部署

- 服务器 47.100.175.214（root 免密）；candle-flow.online → cloudflared → 127.0.0.1:8002；systemd：candle-flow / candle-flow-daily.timer / cloudflared；应用目录 /opt/candle-flow。
- 部署：PowerShell 跑 `scripts/deploy/push.ps1 -Server 47.100.175.214`（勿用 bash 调）。`.env` 与 `data/` 不上传，新机需手工放 `.env`。
- 退出码 1+缺 SETUP_OK ≠ 失败（Tee-Object 捕获超长 stderr 中断）；可靠判据：服务器上 `~/candle-flow-deploy.tgz` 不存在 = 脚本跑完。
- 验证新代码已加载：`systemctl show candle-flow -p ExecMainStartTimestamp` 晚于源文件 mtime → curl 127.0.0.1:8002/api/v1/health → 业务接口跨行业抽样零变化。

## 环境

- bash 需注入 PortableGit PATH 才有 git/ssh。
- 测试：`cd backend && ./venv/Scripts/python.exe -m pytest tests -q`。
- 既有失败（非回归）：test_symbol_search.py 4 个拼音用例 + test_bull_tactics.py::test_scan_market_filters_main_board（缺 pypinyin 资源）。
- 诊断脚本：`_diag_bank.py <symbol>`（模块明细）、`_diag_cycle.py`（周期/红利股估值明细）。
- PowerShell 工具不回显 stdout：Tee-Object 落盘（UTF-16LE）→ iconv 转码读；禁从 bash 调 powershell.exe。
- API 返回 `{code,message,data}`：评分在 `data.composite_score/final_rating`，模块在 `data.modules.<name>`。
