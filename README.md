# 估值模型

各阶段写 JSON，再一次编译公式工作簿。完整模型为 9 张工作表，包含 PE、WACC 和三年 DCF；仅 PE 模式保留原 7 张表。无页面。

## 目录

| 路径 | 内容 |
|---|---|
| `valuation/` | 运行时代码 |
| `valuation/historical_financials/` | 历史财务 |
| `valuation/segment_split/` | 主营拆分 |
| `valuation/segment_research/` | 分部研究 |
| `valuation/operating_cost/` | 运营成本 |
| `valuation/peer_valuation/` | 同业与目标 PE |
| `valuation/dcf/` | DCF取数、假设、计算、WACC及同页敏感性 |
| `valuation/research_dossier/` | 研究底稿 |
| `valuation/workbook/` | 一次出表 |
| `valuation/comein/` | Comein MCP 客户端 |
| `notes/` | 工程化笔记 |
| `output/` | 当前建模产物，不入库 |

## 怎么跑（Terminal 启动指南）

```bash
cd /Users/mezzo/Downloads/估值模型

# 0. 环境准备（首次运行）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. 检查 Comein MCP 连接（只握手 + 列工具，不拉行情）
.venv/bin/python -m valuation.comein ping

# 2. 启动完整估值（全自动一步跑通 10 个阶段，直接生成 Excel 与研究底稿）
# A 股示例：
.venv/bin/python -m valuation --ticker 300750 --name 宁德时代
# 港股示例：
.venv/bin/python -m valuation --ticker hk09992 --name 泡泡玛特
# 美股示例：
.venv/bin/python -m valuation --ticker usAAPL --name 苹果

# 3. 断点续跑（若中途网络中断或某阶段报错，修复后接着跑未完成阶段）
# <run_id> 可填完整目录名（如 20261102_163000_300750）或唯一后缀（如 300750）
.venv/bin/python -m valuation --resume <run_id>

# 4. 仅重新编译并验表（手动修改 output/<run_id>/ 下的 JSON 假设后，秒级重出 Excel，不重跑 Agent）
.venv/bin/python -m valuation --resume <run_id> --rebuild-only

# 为原有七表任务增加 DCF（新任务默认包含；旧任务需明确启用）
.venv/bin/python -m valuation --resume <run_id> --with-dcf
# 仅使用原 PE 模型
.venv/bin/python -m valuation --ticker 300750 --name 宁德时代 --pe-only

# 5. 单独调试/冒烟测试某个阶段
# 仅跑主营业务拆分（输出到 output/smoke/<ticker>）：
.venv/bin/python -m valuation.segment_split --ticker 300308 --name 中际旭创
# 针对已有 run_dir 单独重跑某一个子模块（如运营成本、同业估值、研究底稿）：
.venv/bin/python -m valuation.operating_cost.write --run-dir output/<run_id>
.venv/bin/python -m valuation.peer_valuation.write --run-dir output/<run_id>
.venv/bin/python -m valuation.dcf.write --run-dir output/<run_id>
# 重新取数并生成DCF假设，会覆盖该任务已有的dcf_inputs.json
.venv/bin/python -m valuation.dcf.write --run-dir output/<run_id> --refresh
.venv/bin/python -m valuation.research_dossier.write --run-dir output/<run_id>

# 6. 运行单元测试套件
.venv/bin/python -m unittest discover -s valuation/tests
```

Comein 读 `~/.cursor/mcp.json` 的 `comein-mcp-all`，或环境变量 `VALUATION_MCP_URL` + `VALUATION_MCP_KEY`。LLM 网关配置读项目根目录 `.env`（可参考 `.env.example` 设置 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`VALUATION_MODEL`）。密钥放 `.env`，不要提交。

### 流水线 10 个自动阶段顺序

执行 `.venv/bin/python -m valuation --ticker <代码> --name <名称>` 后，程序按以下顺序自动推进（无人工审核挂起点）：

1. `historical_financials`：拉取近 3 个完整年度财报与最新行情快照，写入 `facts.json`、`company_info.json`。
2. `segment_split`：确认券商拆分口径并直接锁定分部名单（支持 `source_terms` 同义词回指），写入 `split_plan.json`。
3. `historical_fill`：按确认名单补搜各历史年分部收入（券商推算微尾差由 Agent 自行轧平，真实局部披露或年报直接披露差额自动补齐 `XX其他`）。若子项各历史年都因缺数据被填为兜底零，撤销该无依据下钻；该父项没有其余有效具名子项时恢复年报父项及其历史收入，并同步预测名单。有来源支持的真实零收入保留。写入 `segment_split_notes.json`，无依据零占位不得进入预测。
4. `segment_research`：逐分部运行 `brief_agent`（核心分部多轮检索、残差分部收敛至 1~2 次）生成 `research_brief_<分部>.json`，再由 `forecast_agent` 选定预测方法并生成 `forecast_notes_<分部>.json`，最后汇总为 `forecast_notes.json`。
5. `operating_cost`：基于历史成本费用率轨迹检索卖方假设，生成 `cost_assumptions.json`。
6. `peer_valuation`：拉取本公司一致预期（`consensus_estimates.json`），提名同业并拉取 `peForward`；结合已有经营预测、同业财务指标和按需补查的研究，筛选核心池并独立给出合理PE及简要理由，写入 `comps_spec.json`。
7. `dcf`：补取现金流校准数据、国债收益率、Beta序列及附注，整理有来源的假设，生成 `dcf_inputs.json` 和 `dcf_results.json`；原PE模式跳过。
8. `research_dossier`：自动计算共识分歧归因（`consensus_attribution.json`）与全模型快照（`snapshot.json`），由分析师 Agent 一次生成八章深度底稿 `research_dossier.md` 与封面要点 `summary_notes.json`；估值章包含DCF交叉验证。
9. `build`：根据落盘 JSON 一次性编译带动态公式的 Excel 工作表 `<公司名>_估值模型.xlsx`。完整模式在原7表上增加 `DCF估值`，WACC计算位于该页右侧，共8张工作表。
10. `certify`：对生成的 Excel 执行公式引用、跨表锚点与历史/预测勾稽检验，输出 `certify.json`。

## 相对估值

相对估值页在“核心池中位数”下新增“模型给定PE”，备注展示一至两句给定理由。同业均值、中位数继续保留作为参考。下方悲观／中性／乐观及原有系数规则沿用：各情景目标PE＝模型给定PE×情景调整系数，中性默认1，悲观及乐观分别为中性系数的0.85及1.15倍。新任务由估值Agent直接给定基础PE，不再以另一个调整系数重复表达同一判断；模型PE及中性系数均可在Excel调节。快照、总结和研究底稿使用同一判断。

新同业文件包含 `schema_version: 2`、`model_pe`、`model_pe_reason`，必要补查来源保存在 `model_pe_sources`，检索和读取记录保存在 `logs/mcp/peer_valuation_research.json`。新版缺少有效PE或理由会报错；旧任务没有这些字段时仍按历史均值模式复现，新增行明确标注历史规则。要为旧任务生成独立判断，需重跑同业估值、研究底稿并重新编表；只重新编表不会运行估值Agent。

## DCF 与 WACC

显性期复用现有三年收入与经营利润预测，不另造第四、第五年的详细预测。FCFF按 `EBIT－经营现金税＋D&A－Capex－ΔONWC` 计算，D&A及Capex采用收入比率，ΔONWC采用增量收入比率；历史CFO只作校准。估值日处在财年中时，首个未结束财年的FCFF按剩余天数估计，期末折现，不调整季节性。

第三年之后以独立的稳态利润率、税率、再投资比例及永续增长率计算正常化下一年FCFF。WACC采用CAPM股权成本、独立的债务成本和市值资本结构，债务权重使用有息债务总额。经营价值经现金、受限现金、最低经营现金、非经营资产、债务、非控股权益及其他索取权调整后得到归母股权价值。币种及每交易单位普通股比例沿用项目既有定义。

`DCF估值`页内包含WACC×g的5×5敏感性矩阵；每个g会重算稳态收入及再投资，中心格与基准价一致。总结页直接链接DCF价、空间、WACC、g及终值占比。PE继续独立给出主目标价，不自动与DCF加权。

一个估值金融分析师Agent负责完整DCF，重点补搜Capex的投入计划、建设和投产信息，结合历史给出三年及稳态Capex比例、简短判断和实际读取的主要来源。其他参数复用历史、原成本和已有研究，必要时调整，不逐项安排专项检索，也不强制逐项填写判断理由。Capex查询、命中和读取记录保存在`logs/dcf/capex_search.json`，实际使用的来源进入输入文件。已有有效假设作为研究基线；`--refresh`会重新取数并替换假设，不带该选项时仅重算。

所有必要数值仍须有效，不能把缺失数据直接当零。关键项缺失、WACC≤g、币种不明或不适用的金融企业只显示未确认事项，PE照常出表。原始接口响应保存在`logs/dcf/`。手动改动`dcf_inputs.json`后可用`--rebuild-only`重算公式和结果；研究文字为生成时快照，如需更新文字须重跑研究底稿阶段。

DCF估值沿用原模板的无冻结视图、字体和数字格式。历史D&A、Capex及收入占比放在历史财务数据页的现金流历史校准区，供分析师判断预测比例；不自行拼算历史ONWC。DCF仅展示现有三年预测（B—D列）、稳态（E列）和简短备注（F列），首年增量收入直接引用历史财务数据页的上年收入；完整Capex说明和主要来源放在同页下方。WACC的项目、数值、单位和说明放在同页右侧H—K列，G列留空；不再单列WACC工作表。总结与验表根据结果标签解析引用，不依赖旧固定输出坐标。

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

跨币种估值会自动调用进门 `get_economic_indicator_data`，使用中国外汇交易中心的日频“中间价:港元兑人民币”“中间价:美元兑人民币”，覆盖人民币、港元、美元的直接、倒数和交叉换算。按北京时间运行当天查询，优先当天有效报价，周末或节假日回退至近 10 个自然日内的最近有效交易日；交叉换算各段必须同日，不混用不同日期的报价。窗口内无数据时报告缺失，不默认按1换算。同币种不查询汇率。每次获取均重新查询，不读取已有汇率作为缓存；保存的汇率只供本次结果及来源追溯，刷新失败会清除旧换算值。汇率原值、方向、运行日期、实际报价日期和来源保存为 `facts.json` 的 `valuation_fx`，换算系数写入 `market.报表币种兑交易币种`；Excel 和研究摘要同步引用。新任务在财务阶段尝试取汇率，续跑至研究底稿或编表前重新查询，`--rebuild-only` 也会刷新。

A/港/美共用一套七表建表函数，以 A 股现有模板为标准。`valuation/workbook/market_config.py` 只定义适用科目和计算规则，不创建工作表或指定独立版式。历史财务左右分栏，总结左右分栏，成本与利润预测统一历史/预测/备注列；市场差异不再维护为另一套建表脚本。模板修复验收见 [统一模板验收](notes/统一A股建表模板修复验收_20261027.md)。

机构营收预测直接读取财务阶段已确定的模型币种，不在此重新识别或修改模型币种。逐年机构明细的 `currency` 或币种型 `unit` 优先于上层元数据及汇总预测币种；同币种只统一金额单位，不同币种则独立按需查询人民币、港元、美元汇率，不依赖已保存的目标价汇率。汇率沿用运行当天优先、前一个自然日兜底、同日换算和每次重新查询的规则。转换后按机构、指标和年度选最近有效报告，再计算中位数；新报告缺少某年数据时保留旧报告中的有效值，逐值记录实际报告日期。EPS覆盖不影响营收写入。来源金额、币种、换算日期和汇率保留在逐项来源与日志中。若原接口有营收预测却全部无法处理，机构预测阶段明确报错；原接口本来没有营收预测则保留缺失。旧JSON和工作簿不会随代码修改自动重算，需要重新运行机构预测阶段再出表。
