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

`historical_financials` 拉财报，`segment_split` 只定名单，不定方法；确认后再由模型按计划补搜并拍出历史分部收入，代码只校验加总对得上营业收入。分部研究按分部先由 brief 收四类材料，再由 forecast 选方法并给出我们的预测。运营成本由公司级 agent 按历史轨迹检索卖方后落假设。一致预期和同业在同一步：代码拉本公司盈利预测，同业预测首年 PE 取 pricePerformance.peForward，agent 只提名和分核心。研究底稿由分析师 agent 结合已有 brief 一次生成 Markdown 和 `summary_notes.json`；底稿写明研究口径、最新经营验证、反证与跟踪，摘要填入 Excel 总结叙述区。`--rebuild-only` 不重写底稿。不加 `--auto-approve` 时，改完 JSON 再 `--resume`。

## 产物

完整建模写在 `output/<日期>_<代码>/`：

- `facts.json`、`split_plan.json`、`forecast_notes_<分部>.json` 等阶段 JSON
- `snapshot.json`、`summary_notes.json`
- `<股票名>_估值模型.xlsx` 带公式工作簿
- `research_dossier.md`
- `certify.json`
- `logs/`

## 港美股适配

使用相同入口，证券代码如 `hk09992`、`usAAPL`。A 股继续原路径。港美底稿按实际财年、币种和适用费用布局输出，缺失统一显示右对齐的“-”；美股科目为“营业利润（SOPR）”“基本每股收益（扣非）”。未来假设由原研究流程产生，历史数据不由模型补造。

完整估值沿用收入到基本 EPS 的利润链。港美默认与 A 股一样，预测 EPS 直接引用财务数据页的快照股本并假设持平，不再新增预测股数和普通股利润行。财务费用、营业外收支及其他经营净收益复用原有字段；联营损益、优先股股息仅在实际适用时列示，不固定预留零行。旧版非零合并调整需先迁移为有来源的具体项目，不能静默丢弃。`facts.json` 的 `currency` 是实际财务金额币种，`trading_currency` 是交易币种；`market.报表币种兑交易币种` 为每单位财务币种兑换的交易币种，`market.每交易单位普通股数` 为普通股/ADS 比例，须有来源。同币种无需额外汇率。ADS 的 EPS 分母按快照股本及比例换成普通股数，目标价再转为每 ADS，避免重复换算。比例或币种未确认时会提示缺口。同业 PE 采用接口最新预测年数值，A/港/美统一按首个预测年度显示为 `20xxE PE`。备注共用业务可比点、主要差异和该年估值水平的规则，不再逐行追加接口缺元数据提示；已有明确的财年或盈利口径冲突仍校验，来源字段仍保留供追溯。

少数股东损益优先取来源值，其次在同口径下用合并净利润减归母净利润；名称不加计算标识。美股通用归母字段调整范围未确认时暂不相减。实际覆盖和验收结果见 [改造验收](notes/港美股估值模型改造验收_20261027.md)。

跨币种估值会自动调用进门 `get_economic_indicator_data`，使用中国外汇交易中心的日频“中间价:港元兑人民币”“中间价:美元兑人民币”，覆盖人民币、港元、美元的直接、倒数和交叉换算。按北京时间运行当天查询，优先当天有效报价，缺失时仅回退前一个自然日；交叉换算各段必须同日，不混用当天与前一天的报价。当天与前一天都无数据时报告缺失，不自动延伸到更早交易日，也不默认按1换算。同币种不查询汇率。每次获取均重新查询，不读取已有汇率作为缓存；保存的汇率只供本次结果及来源追溯，刷新失败会清除旧换算值。汇率原值、方向、运行日期、实际报价日期和来源保存为 `facts.json` 的 `valuation_fx`，换算系数写入 `market.报表币种兑交易币种`；Excel 和研究摘要同步引用。新任务在财务阶段尝试取汇率，续跑至估值审核、研究底稿或编表前重新查询，`--rebuild-only` 也会刷新。

A/港/美共用一套七表建表函数，以 A 股现有模板为标准。`valuation/workbook/market_config.py` 只定义适用科目和计算规则，不创建工作表或指定独立版式。历史财务左右分栏，总结左右分栏，成本与利润预测统一历史/预测/备注列；市场差异不再维护为另一套建表脚本。模板修复验收见 [统一模板验收](notes/统一A股建表模板修复验收_20261027.md)。

机构营收预测直接读取财务阶段已确定的模型币种，不在此重新识别或修改模型币种。逐年机构明细的 `currency` 或币种型 `unit` 优先于上层元数据及汇总预测币种；同币种只统一金额单位，不同币种则独立按需查询人民币、港元、美元汇率，不依赖已保存的目标价汇率。汇率沿用运行当天优先、前一个自然日兜底、同日换算和每次重新查询的规则。转换后按机构、指标和年度选最近有效报告，再计算中位数；新报告缺少某年数据时保留旧报告中的有效值，逐值记录实际报告日期。EPS覆盖不影响营收写入。来源金额、币种、换算日期和汇率保留在逐项来源与日志中。若原接口有营收预测却全部无法处理，机构预测阶段明确报错；原接口本来没有营收预测则保留缺失。旧JSON和工作簿不会随代码修改自动重算，需要重新运行机构预测阶段再出表。
