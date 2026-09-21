# 估值模型

各阶段写 JSON，再一次编译 7 张公式 sheet。无页面。

## 目录

| 路径 | 内容 |
|---|---|
| `valuation/` | 运行时代码 |
| `valuation/historical_financials/` | 历史财务 |
| `valuation/segment_split/` | 主营拆分 |
| `valuation/segment_research/` | 分部研究 |
| `valuation/operating_cost/` | 运营成本 |
| `valuation/peer_valuation/` | 同业与目标 PE |
| `valuation/research_dossier/` | 研究底稿 |
| `valuation/workbook/` | 一次出表 |
| `valuation/comein/` | Comein MCP 客户端 |
| `notes/` | 工程化笔记 |
| `output/` | 当前建模产物，不入库 |

## 怎么跑

```bash
cd 估值模型
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 连 Comein MCP（只握手 + 列工具，不拉行情）
.venv/bin/python -m valuation.comein ping

# 完整活数据建模
.venv/bin/python -m valuation --ticker 300750 --name 宁德时代

# 只测收入拆分（不跑完整建模）
.venv/bin/python -m valuation.segment_split --ticker 300308 --name 中际旭创

# 改 JSON 后只重编译，不重跑各阶段
.venv/bin/python -m valuation --resume <run_id> --rebuild-only
```

Comein 读 `~/.cursor/mcp.json` 的 `comein-mcp-all`，或环境变量 `VALUATION_MCP_URL` + `VALUATION_MCP_KEY`。旧名 `SPIKE_*` 仍可用。密钥放 `.env`，不要提交。

`historical_financials` 拉财报，`segment_split` 只定名单，不定方法；确认后再由模型按计划补搜并拍出历史分部收入，代码只校验加总对得上营业收入。分部研究按分部先由 brief 收四类材料，再由 forecast 选方法并给出我们的预测。运营成本由公司级 agent 按历史轨迹检索卖方后落假设。一致预期和同业在同一步：代码拉本公司盈利预测，同业预测首年 PE 取 pricePerformance.peForward，agent 只提名和分核心。研究底稿由分析师 agent 先写 Markdown，再抽出 `summary_notes.json` 填总结叙述区；`--rebuild-only` 不重写底稿。不加 `--auto-approve` 时，改完 JSON 再 `--resume`。

## 产物

完整建模写在 `output/<日期>_<代码>/`：

- `facts.json`、`split_plan.json`、`forecast_notes_<分部>.json` 等阶段 JSON
- `snapshot.json`、`summary_notes.json`
- `<股票名>_估值模型.xlsx` 带公式工作簿
- `research_dossier.md`
- `certify.json`
- `logs/`
