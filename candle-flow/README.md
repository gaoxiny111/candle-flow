# Candle Flow 蜡烛图交易系统

基于史蒂夫·尼森《日本蜡烛图教程》的形态识别与交易辅助：FastAPI + Vue3 + lightweight-charts + SQLite + AKShare。

## 功能特性

- **K 线**：AKShare / 东财同步，A 股含当日行情；失败时保留本地数据
- **形态引擎**：锤子/上吊/流星/倒锤、十字族、吞没、启明星/黄昏星/弃婴、红三兵/三只乌鸦/前进受阻/停顿、刺透/乌云、孕线、捉腰带、反击、平头、分手线、跳空并列、两只乌鸦、三星、塔形、三法、窗口及窗口回测、脱离线、独特三川、藏婴吞没、跳空肩带、突破缺口
- **尼森规则**：周线定趋势、日线找入场（周线向下不做多、向上不做空）；波段趋势（非仅均线）；锤子/上吊等须下一根收盘确认；形态极值止损；收盘价才算突破/填窗；红三兵不追高；蜡烛形态触发，均线金叉死叉/布林/MACD/RSI/量能/窗口只做汇聚确认
- **交易信号**：评分 ≥ 60 且汇聚 ≥ 2；止盈优先第十六章测幅（箱体/对等运动/旗形·三法），否则 2R/3R；确认后跟踪持仓（止损看影线，目标/2R 看收盘）
- **图表**：主图 MA / 布林 / 窗口带；副图 MACD、RSI、ATR；日线/周线切换（周线定趋势）
- **回测**：按当前规则在已同步日线上顺序模拟
- **宽基主力**：沪深 300 / 中证 500 / 1000 / 2000 / 科创 50 分时资金对比

## 快速开始 (Windows)

```bat
install_windows.bat   # 安装依赖并初始化数据库
start_all.bat         # 开发模式：Vite + uvicorn（两个端口）
```

- 前端开发：http://localhost:5173 （若占用则 5174）
- 后端 API：http://localhost:8000
- API 文档：http://localhost:8000/docs

### 单端口给别人用（本机 + 隧道）

```bat
cd frontend
npm run build
cd ..\backend
venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8000
```

有 `frontend/dist` 时，FastAPI 同时提供页面和 `/api/v1`。再用 Cloudflare Tunnel / Tailscale 把 8000 发出去。电脑要保持开机。

设置页首次需设口令（至少 4 位），之后改交易偏好要验证。口令哈希存在本机 SQLite，不是「任意 Token」。

## 项目结构

```
candle-flow/
├── backend/          # FastAPI
│   └── app/core/     # 形态、窗口、汇聚、尼森规则
├── frontend/         # Vue3
├── install_windows.bat
└── start_all.bat
```

## 技术栈

- 后端: Python 3.10+, FastAPI, SQLAlchemy, SQLite, AKShare
- 前端: Vue 3, TypeScript, Pinia, lightweight-charts, Vite
