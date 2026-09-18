# candle-flow 项目长期记忆

## 项目性质

A 股基本面 / 技术面分析系统。后端 FastAPI + SQLAlchemy（`backend/app`），Python 3.13（`backend/venv`）。
基本面分析核心在 `backend/app/analysis/`：`financials.py` 取数建表 → `engine.py` 编排 → `modules/*.py` 各维度打分 → `models/*.py` 相对/绝对估值。

## 基本面分析口径铁律（改动必须遵守）

1. **绝不改写真实披露值。** 展示字段保持原始精度；任何折减/抑制只落在独立的 forward 字段上。
   （历史事故：`profit_yoy` 被硬截断成 300，导致 493.25% 的中报增速失真，PEG 建立在错误分母上。）
2. **金额单位统一走 `cashflow._fmt_yi()`。** 禁止对「亿」级金额用 `:.0f`——小值会被舍成 0，负值若不按 `abs()` 判断量级更会漏掉 `/1e8` 换算，输出「-1119194300亿」这种荒谬量级。
3. **「以最新报告期为锚」。** 中报/季报已披露时，资产负债率、经营现金流/净利润等比率都必须取最新报告期，不得回退到年报。
4. **比率口径要能自证。** 每个关键比率同时记录 `*_period` / `*_source`（如 `debt_ratio_scope`、`latest_cash_ratio_source`），便于报告里标注口径。
5. **单一比率不做一票否决。** ROIC<WACC、现金流为负这类判定必须带商业模式认知与多期归一化，不能单期定生死。
6. **综合分只允许有限度惩罚，禁止无上限乘法叠加。** 已有惩罚层：cashflow 权重 2 倍(0.32)、E 档 0.5 打折、cashflow_veto 降档、value_trap_veto 锁 28、compliance_veto 强制 E。风险乘数已限幅 `RISK_MAX_PENALTY=0.25`。新增惩罚前先确认不构成三重计数。
7. **指标名/展示值/得分必须同口径。** 出现过 `name=(5年均值)` + `value=1.16` + `score=25(当期口径)` 的自相矛盾报告。按哪个口径打分，标签与 value 就同步切到哪个口径。
8. **给模块新增依赖的上下文字段时，务必确认 `engine.py` 的 `ctx` 真的传了。** 历史事故：`ctx` 长期漏传 `symbol`，各模块 `kwargs.get("symbol")` 恒为 None。
9. **同一判定不要在两处各算一遍。** 估值模块与引擎曾各自调 `classify_growth_stock` 且传参不同，导致「同股两判」，PEG 静默失效。
10. **毛利率必须按商业模式取阈值**：分销 `((12,100),(8,12),(4,8))`，制造业 `((50,100),(30,50),(15,30))`。
11. **绝对阈值必须按商业模式校准，低净利率高周转行业用同业相对基准。** 分销/贸易的 ROIC / ROE / 净利率 / 资产负债率 / OCF比 一律以 `DISTRIBUTION_BENCHMARKS` 的同业中位数为锚（达到中位 ≈ 55 分），不得直接套用制造业阈值——实测同业 ROIC 三年均值全部 1.5%~3.8%，无一达到 WACC≈10%，绝对阈值会把整个行业判成不合格。
    但**现金流质量例外**：经营现金流为负不是模式固有属性（同业润欣科技 OCF/净利 +1.42 证明可以做到正），仍以绝对口径为主。
12. **相对评分必须保留区分度。** 引入行业基准后要验证「行业内最差者仍被判低分」（如中电港 ROIC 1.50% 得 35 分），否则等于把整个行业一起豁免。
13. **模块下限保护必须留痕，且它是「指标级加分失效」的常见原因。** `cashflow` 有两条保底（`healthy_wc → 58`、`dist_expanding → 52`），一旦触发，指标层再怎么加分模块分都不动。故 `metadata` 必须带 `raw_weighted_score` 与 `floor_applied`。
    **诊断心法**：凡遇到「某维度分数纹丝不动 / 正好卡在 50~58 的整数」，先算 `Σ(score×weight)/Σweight`，确认是不是下限在兜底，**不要先去改指标**。（深圳华强现金流 52.0 = 下限，原始加权仅 47.2。）
14. **同一批数据不得在两个模块得出相反定性。** 已发生：`cashflow` 的「分销模式观察项」说应收账期「基本稳定」，`risk` 却按 `is_quality_receivable_context()` 判「回款极其困难」并扣 15 分。
    根因是该函数只认央企名单/行业关键词/分红资产/现金覆盖，**漏了分销模式**。现 `ar_turnover_warning()` 已带 `distribution` 参数（保留营运资本占用提示与轻扣 6/8，不作回款危机定性），两模块都必须传。
    通用教训：**凡输出「定性化措辞 + 扣分」的函数，都要检查是否漏了 `is_distribution` 分支。**
15. **措辞必须与事实一致，尤其是「对照锚」类文案。** 曾出现上年年报 OCF/净利 = -2.14（同样不健康）时仍写「勿被上年年报高含金量掩盖」。写分支文案时先判断被引用方到底健不健康，不要默认它健康。
16. **部分科目求和得来的代理比率，分子为 0 是「数据缺失」而不是真实读数。** 已发生：`financials.py` 里 `current_ratio = (货币资金+应收+存货)/(经营性负债+短借)`，银行这三个科目全未映射 → `0/511亿 = 0.0` → 按 0 分打最差档（**20 家银行 CR/QR 全部为 0.0**）。凡此类比率，分子为 0 一律返回 `None`，消费端也把 `<= 0` 视为缺失。
17. **指标口径必须与本行业会计科目对应，错配可能是双向的。** 银行四项偿债指标里 3 项误低（资产负债率天然 90%+ 触发 `dr>75→25分DANGER`、流动/速动比率无对应科目）、1 项误高（`interest_bearing_debt()` 只算短借+长借+应付债券，**不含吸收存款**，招行 0.99% 拿 90 分）。新增行业豁免时逐指标自查「该行业有对应科目吗、错在哪个方向」。
18. **行业关键词不是同义词表：语义相邻但监管指标不同源的行业不得并入同一口径。** 银行只认「银行」；证券/保险**没有**不良贷款率与拨备覆盖率，若并入银行监管口径五个子项全取缺省值 ≈ 常数 52 分，会把中信证券 63.1→~52、中国平安 72.2→~52（比现状更差）。改动后必须逐项验证非目标行业未被误伤。
19. **综合分除了模块加权，还有「公告事件型扣分」，且事件有时效性。** `engine.py:431` 按 `composite -= min(30, 8 × 未释放的观察级事件数)` 直接扣综合分。事件靠**公告标题关键词**识别，「已结束」的事件必须通过 `RISK_RELEASE_KEYWORDS` 释放，否则会持续误扣。已发生：关键词只列「减持计划届满」，而真实公告写「减持计划**期限**届满暨减持结果」→ 少「期限」两字匹配失败，格力电器减持已到期结束仍扣 8 分。**枚举式关键词追不上标题写法，应改用语义组合规则（见 `_is_release_title()`：同时含「减持」与「届满/到期」）**，并加边界测试保证不误释放「拟减持」「限售期届满」。
20. **怀疑数据源出错前，必须先用外部信源交叉验证。** 格力 797.19 亿短期借款看似异常，核实后完全属实（占总资产 19.97%），且「存贷双高」是市场公认问题。**若不核实就"修正"，会把真实事实改成失真。** 另外行业新闻常提供方案刻意回避的反面证据（格力：合同负债 152→84.74 亿腰斩、OCF 同比 -33.6%、中期不分红）。
21. **用户方案给出的"旧综合分"几乎都不可信，必须独立复算。** `MODULE_WEIGHTS` 是 `{profitability 0.2, growth 0.16, solvency 0.16, cashflow 0.32, valuation 0.16}`（**非等权**），另有 E 档打折、风险乘数、事件型扣分。格力案例：方案五维旧值与 `dim_scores` 逐位一致，却按等权算出"76.1"，真实 59.7（差 16 = 2 个观察事件扣分）。

## 评分链路备忘（改评分前必读）

`composite = Σ(_penalized_module_score(score) × w) / Σw`，随后按序：`risk 限幅调整` → 高成长双强保护(≥70) → `value_trap_veto`(锁 ≤28) → `cashflow_veto`(降一档) → `compliance_veto`(强制 E) → **观察级事件扣分 `-= min(30, 8 × 未释放事件数)`**（`engine.py:421-431`；事件由公告标题匹配得到，已结束的必须被 `RISK_RELEASE_KEYWORDS` 释放，否则持续误扣）。
`MODULE_WEIGHTS`：profitability .20 / growth .16 / solvency .16 / **cashflow .32** / valuation .16（efficiency、industry 只展示不计权）。
常量：`RISK_THRESHOLD=60`、`E_GRADE_SCORE=40`、`E_GRADE_PENALTY=0.5`、`CASHFLOW_VETO_THRESHOLD=40`、`RISK_MAX_PENALTY=0.25`。
`rating_label`：≥85 A / ≥80 A- / ≥74 B+ / ≥70 B / ≥65 B- / ≥55 C / ≥40 D / <40 E。

## 商业模式画像（`analysis/config/company_profiles.py`）

- 入口：`get_company_profile`、`business_model_of`、`is_distribution`、`peers_for`、`get_merger_consolidation`。
- **分销/贸易模式**（`business_model="distribution"`）：低净利率、高周转，上游预付 + 下游长账期，扩张期经营现金流为负是行业常态。识别优先级：profile 显式声明 > `ELECTRONIC_DISTRIBUTION_PEERS` 名单（按代码，经 `_norm_code` 归一化）> 行业关键词。
- 影响六处：`cashflow` 下调阈值 + 输出「分销模式观察项」+ 应收告警带 `distribution`；`profitability` 毛利率用分销阈值、WACC 取 10.0%、ROE/ROIC/净利率走 `_score_vs_benchmark` 相对基准、`roic_below_wacc=False`；`solvency` 资产负债率走同业中位锚、不套用「电子/轻资产」严阈值；`risk` 应收告警带 `distribution`；`comps` 用显式同业清单。
- **同业基准** `DISTRIBUTION_BENCHMARKS`（7 家分销商 2026 中报实测中位数，含原始分布注释）：ROIC单期 4.33 / ROIC3年均 3.32 / 净利率 1.91 / ROE 5.35 / 毛利率 9.58 / OCF比 -2.25 / 资产负债率 61.42。原「ROE>=8% 才豁免」的门槛对分销业过高，已改为「ROIC 与 ROE 均不低于同业中位」。
- 分销同业名单：000062 深圳华强 / 001287 中电港 / 001298 好上好 / 300131 英唐智控 / 300184 力源信息 / 300475 香农芯创 / 300493 润欣科技 / 301099 雅创电子。
- **银行商业模型（2026-09-18 新增）**：`business_model_of` 返回 `"bank"`（仅 `BANK_INDUSTRY_KEYS=("银行",)`），
  基准 `BANK_BENCHMARKS`（`bank_benchmarks()`）= 2026H1 实测 20 家 A 股银行：DR 中位 **91.94%**、权益占比中位 **8.06%**
  （min 90.18 招行 / p25 91.62 / p75 92.96 / max 93.95 邮储）。
  偿债口径：`score = clamp(58 + (权益占比 - 8.06) * 5.0, 35, 76)`，**跳过流动/速动比率与有息负债率**，
  `dr>75` 的 DANGER 覆盖对银行失效；metadata 打标 `bank_leverage_only=True` /
  `asset_quality_metrics_available=False`，并固定输出「数据源不提供不良率/拨备/资本充足率」声明。
  实测：招商银行 33.3 E → **66.8 C**（综合 71.9→79.9）、工商银行 33.3 E → 55.8 C；
  20 家银行分布 47.9(邮储)~66.8(招行)，极差 18.9 分保留区分度。
  **注意：本数据源无任何银行专属指标（不良率/拨备/资本充足率/净息差），不要写依赖这些字段的逻辑。**
  当前评分分布（2026-09-18 线上实测，供一致性对照）：香农芯创 68.3 / 力源 66.0 / 商络 62.9 / **深圳华强 61.3** / 雅创 60.4 / 润欣 58.5 / 中电港 50.7 / 好上好 50.4 / 英唐智控 30.2（现金流 E 降档）。
  注：同业分数会随行情/PE 实时小幅漂移（实测同日内 0.5~1.6 分），做前后对比时先确认代码路径是否真被触达，别把数据漂移当成回归。
- **哪些分销商会走「分销应收告警」分支**（2026-09-18 实测）：只有 000062 / 300975 / 001298 三家。其余标的不触发，改动对其无影响。
- **深圳华强余下短板（勿再试图靠口径松动消掉）**：自由现金流 -11.4 亿、每股经营现金流 -0.763、OCF/净利 -2.04、PE_TTM 39。
  现金流模块已被 `dist_expanding` 下限保护（原始加权 47.2 → 52.0）。
- **已配置画像**：商络电子（300975，分销；并表标的广州立功电子，2026 起纳入合并）、贵州茅台（白酒风险块）。

## 部署（生产服务器）

- **服务器**：`47.100.175.214`（阿里云 Ubuntu，root + `~/.ssh/id_ed25519` 免密）。
- **对外域名**：`https://candle-flow.online` / `https://www.candle-flow.online` → cloudflared 隧道 → `127.0.0.1:8002`（uvicorn）。
- **服务**：`candle-flow`（API）、`candle-flow-daily.timer`（定时任务）、`cloudflared`（隧道），均为 systemd 单元。
- **应用目录**：`/opt/candle-flow`；生产库 `/opt/candle-flow/backend/data/candle_flow.db`。
- **部署命令**（Windows PowerShell，必须用 PowerShell 而非 bash，脚本依赖 robocopy）：
  ```powershell
  & "D:\candle-flow(2)\candle-flow\candle-flow\scripts\deploy\push.ps1" -Server 47.100.175.214
  ```
- **安全设计**：`.env` 与 `data/` 被刻意排除在上传外，不会被覆盖；故**新机首次部署前必须手工放好** `/opt/candle-flow/backend/.env`。
- **已知良性告警**（勿误判为失败）：本机缺 `~/.cloudflared` 凭据时的 `WARN: keep server copy`；cloudflared 启动初期 quic dial timeout 几秒后自愈。
- **PowerShell 工具不回显 stdout**：必须 `*>&1 | Tee-Object -FilePath $log` 落盘，再用 `iconv -f UTF-16LE -t UTF-8` 转码读取。
- **⚠️ 退出码 1 且日志缺 `SETUP_OK` ≠ 部署失败**（2026-09-18 踩过）：根因是 PowerShell 侧
  `Tee-Object` 捕获超长 stderr（cloudflared usage 文本）时中断，SSH 会话其实已跑完。
  **唯一可靠判据**：`push.ps1` 外层最后一行 `rm -f $HOME/candle-flow-deploy.tgz` 只在
  `sudo bash remote-setup.sh` 返回 0 时才执行 —— 上服务器 `ls ~/candle-flow-deploy.tgz`，
  **不存在即代表脚本完整跑完**。不要凭退出码盲目重跑。
- **验证"服务是否真的加载了新代码"**：比 `systemctl is-active` 更可靠的是比时间戳 ——
  `systemctl show candle-flow -p ExecMainStartTimestamp` 必须**晚于** `stat` 出来的源文件 mtime，
  否则进程还跑着旧模块（restart 那步没执行）。
- **改完代码上线流程**：本地 `pytest tests -q` 跑通 → `push.ps1` → 三段验证：
  ① `systemctl is-active` + `ExecMainStartTimestamp` 对比源文件时间；
  ② `curl 127.0.0.1:8002/api/v1/health`；
  ③ 业务接口 `GET /api/v1/analysis/{symbol}?refresh=true`（本地/服务器/公网三方对齐）；
  ④ **跨行业抽样**确认非目标行业零变化（证明改的是真缺陷而非集体放水）。

## 环境备忘

- Bash 工具需显式注入 PATH 才能用 git：`git.exe` 位于 `/c/Users/14149/.workbuddy/binaries/PortableGit/versions/1.2.0/cmd`（**不在** `bin/`）。
  完整可用前缀：`export PATH="/c/Users/14149/.workbuddy/binaries/PortableGit/versions/1.2.0/cmd:/c/Users/14149/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Windows/System32:/c/Windows"`
- 后端测试：`cd backend && ./venv/Scripts/python.exe -m pytest tests -q`
- 既有失败项（与业务改动无关，勿误判为回归）：`tests/test_symbol_search.py` 的 4 个拼音用例 + `tests/test_bull_tactics.py::test_scan_market_filters_main_board`，根因是环境缺 `pypinyin` 资源，非代码问题。

## 工具环境坑（踩过，别再踩）

- **PowerShell 工具不回显 stdout**（连 `Write-Output` 都没有），但命令**确实执行**。变通：`Tee-Object` / `Add-Content` 落盘 → 用 Read 读；PS 5.1 落盘为 **UTF-16LE**，需 `iconv -f UTF-16LE -t UTF-8` 转码，否则 Read 报 "binary file"。
- **禁止从 bash 调 `powershell.exe`**：被安全策略拦截（"bypasses PowerShell security checks"）。部署走 PowerShell 工具。
- Bash 默认 PATH 损坏（`dirname`/`ls` 找不到），ssh/curl 等需注入：
  `export PATH="/c/Windows/System32/OpenSSH:/c/Users/14149/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"`
- 远端解析 JSON：`ssh host 'cat > /tmp/x.py <<"PY" ... PY; python3 /tmp/x.py'`（单引号 heredoc，避开多层引号转义）。
- `GET /api/v1/analysis/{symbol}` 返回体是 `{"code","message","data":{...}}`：评分在 `data.composite_score` / `data.final_rating`，维度在 `data.dim_scores`，模块在 `data.modules.<name>`。
