# 水果数据全量实验

[runtime.fruit-full.yaml](../../route2/config/runtime.fruit-full.yaml) 使用本地合成数据 `route2/local_data/fruit_data_v2`，共 23 表、1,102,238 行。输入包括表结构、声明约束、外键文件及全部 CSV 行。业务抽取走 `source_mapping_only` 后的字段关联、定义证据包、概念与关系判定流程，知识库与外部本体关闭。

这里的 full 表示完整导入、完整字段统计与定义索引，并保留预算内的全部候选及未决状态。各项上限仍会生效，超限必须查看 coverage。配置不逐条复制百万物理记录的全部属性：`materialize_all_objects: false`，每表保留 3 条预览；接受的定义成员、关系引用记录和有证据的事实实例另行保存。候选不等于业务断言。

## 运行

以下命令从仓库根目录开始。输出目录必须不存在。

```sh
cd route2
uv sync --locked --extra embedding
uv run python scripts/generate_fruit_data.py validate --input local_data/fruit_data_v2
```

如果还没有输入，先生成一次，再执行上面的校验。生成器采用固定规则，无随机种子参数；默认 scale 1.0 写入上述完整规模，已有目录不会被覆盖。

```sh
uv run python scripts/generate_fruit_data.py generate --output local_data/fruit_data_v2 --scale 1.0
```

在本地 `route2/.env` 中配置下列变量。API Key 不写入 YAML、命令历史或文档。模型字段应填服务端实际可用的 `deepseek-v4-flash` 名称或对应部署 ID；实验记录以 manifest 中实际模型名为准。

```dotenv
ONTOLOGY_LLM_MODEL=deepseek-v4-flash
ONTOLOGY_LLM_BASE_URL=<服务端提供的 OpenAI 兼容地址>
ONTOLOGY_LLM_API_KEY=<本地凭据>
ONTOLOGY_EMBEDDING_MODEL_PATH=/完整本地路径/Qwen3-Embedding-0.6B
```

本配置为本机 Qwen3-Embedding-0.6B 目录提供了默认路径，环境变量可覆盖。也可省略最后一个变量使用配置默认值。模型仅从本地加载，不自动下载；向量编码的对象是定义模式代表，未对百万输入行逐条编码。

```sh
ONTOLOGY_LLM_STREAM=false \
ONTOLOGY_LLM_THINKING_MODE=disabled \
ONTOLOGY_LLM_THINKING_PARAMETER=thinking \
ONTOLOGY_EMBEDDING_ENABLED=true \
uv run ontology-r2 build \
  --config config/runtime.fruit-full.yaml \
  --output runs/fruit-full-deepseek-20260927
```

命令显式设置传输和向量开关，避免其他非空环境变量覆盖 YAML。DeepSeek 请求发送 `thinking.type=disabled`，每次请求超时为 300 秒，最多输出 12,288 tokens。结构化返回使用代码已有的 `tool_choice=auto`，没有额外的 `auto` 配置键。所有阶段共用 800 次模型调用预算，重试也受共享预算约束；90,000,000 是程序按输入字节和最大输出预留的保守计数，不能当作实际计费 token。

元数据图和 `datahub-metadata.json` 在构建时自动导出。可再次导出到另一个文件：

```sh
uv run ontology-r2 export-datahub \
  --run runs/fruit-full-deepseek-20260927 \
  --output runs/fruit-full-deepseek-20260927/datahub-export.json
```

这一步只生成 DataHub 兼容格式，本地查询和可视化使用本地图，不需要 Docker、DataHub Lite 或 DataHub 服务参与推断。

## 预算与覆盖范围

| 阶段 | 本配置 | 需要检查的结果 |
| --- | --- | --- |
| 输入与卡片 | 全输入；1,200,000 张卡片；每字段 1,024 字符；每表最多 16 个未知字段 | `semantic_card_coverage.yaml` 的扫描行数、`rows_omitted_cap`、未检查列与截断信息 |
| 字段关联 | 10,000 候选与 10,000 次完整输入核验；每来源表最多 10,000 次；条件探测 2,000 对、保留 4,000 条条件候选 | `discovery_coverage.yaml` 的未核验、未探测、全局排队和核验错误 |
| 别名与规则 | 最多 1,000 对别名线索、32 次完整别名核验；14,000 条规则变体保留上限 | `alias_candidates.yaml` 的采样与遗漏；`association_rules.yaml` 的 unresolved、未导出变体与错误 |
| 关联 Agent | 最多 3 次模型调用、12 次工具调用、4 次完整扫描，总超时 900 秒 | Agent 的停止原因；建议只有完整核验后才成为可执行技术规则 |
| 语义证据包 | 2,000 个概念模式、1,000 个关系包，最多判定 3,000 包；生成与审查批量上限均为 6；每包最多修复 1 次 | 模式未处理量、组包失败、模型预算耗尽、审查拒绝；包太大时批量会自动缩小 |
| 可选语义阶段 | 上位类型 20、同名等价 40、配置关系 40、事实新类型 32、事实字段绑定 48 次决定上限 | 各阶段 coverage 与全局 800 次预算；这些上限不是预留配额 |
| 定义成员与事实 | 成员检查上限 1,200,000；事实每表最多 50,000 个真实元组候选，完整分组阈值 1,200,000 | 引用变体未决、类型不明、坐标冲突、单位缺失和实例源行数超限 |

字段值关联仍有明确的适用范围：对符合条件的非空字段做完整 DISTINCT，短值最大 96 字符；高频公共值和超长值的排除会记录。别名线索最多读取每表 10,000 行、每字段 256 个值，因此必须在完整输入上重新校验，不能将样本重合直接当关系。扩大的候选预算不保证所有关联都被接受。

## 恢复与复核

保留原运行目录，向新的目录恢复：

```sh
ONTOLOGY_LLM_STREAM=false \
ONTOLOGY_LLM_THINKING_MODE=disabled \
ONTOLOGY_LLM_THINKING_PARAMETER=thinking \
ONTOLOGY_EMBEDDING_ENABLED=true \
uv run ontology-r2 build \
  --config config/runtime.fruit-full.yaml \
  --resume-from runs/fruit-full-deepseek-20260927 \
  --output runs/fruit-full-deepseek-20260927-resume1
```

恢复会重新核对输入快照、根模型、提示词、实际模型与字段排除策略。已完成且契约一致的语义结果可复用；未完成阶段继续处理。字段模板可跨数值快照复用，但会重新核验结构、列角色、关联条件和单位，并生成当前快照证据。若要显式跨快照使用模板，在本地配置中设置 `fact_templates_from`，不要用 `--resume-from` 绕过快照检查。

实验结束检查五项：

1. manifest 输入规模、文件哈希、实际模型、传输参数与预算符合本次设置；失败、预算耗尽、未扫描均有记录。
2. extraction plan、ontology、证据分片和 manifest hash 一致；阶段后续失败仍保留先前接受结果。
3. 用独立抽样清单核验类型分类、指标经营对象、范围、单位和计算口径，缺证据保持未知。
4. 用否定、条件、同名异义、公式操作数和仅技术关联的反例核验关系；不得把它们扩成无条件业务关系。
5. 按 accepted、unresolved、rejected、未扫描分别给分母。程序测试、HTTP 成功、候选数和图节点数不等于语义准确率。

## preflight v3 的依据

本地 `runs/association-rebuild-preflight-v3-20260927/summary.yaml` 记录：23 表、1,102,238 行、0 次模型调用、882.5 秒。该轮未启用向量编码，不能用它估算完整模型阶段耗时。

- 全输入生成 598,511 张卡片、580 个定义模式；卡片上限未丢行，索引文本未截断。14,900 行业务事实从定义索引分流，不能据此声称已经形成事实实例。
- 580 个定义模式全部组包，另有 92 个关系包，共 672 包。24,200 个定义卡片变体未分别送模型，后续要看定义成员核验和引用变体未决量。
- 2,187 个关联候选只核验 1,000 个，留下 1,187 个未核验；条件探测留下 1,566 对未探测、1,211 条条件候选排队。
- 规则输出上限 1,000 导致 1,283 个变体未导出；已有 121 条 checked_technical、92 条 observed_subset。新配置提高保留上限，并将别名完整核验限定为 32 次；剩余项必须保留 unresolved。

进度日志记录字段统计约 13 秒、候选召回约 5 秒、1,000 次字段核验约 13 秒、卡片索引约 185 秒。其余时间包含别名核验、组包及产物写入，现有日志不能精确拆分。全量实验增加了条件探测、字段核验和向量编码；别名完整核验从预检的 96 次改为有界的 32 次，剩余候选仍须显式报告。

672 个包按每批 4 个、生成与审查各一次计算，理想下限约 336 次模型调用；实际会因包大小、修复和其他阶段增加，800 次预算仍可能产生 partial。以上预检记录只用于容量和覆盖安排，未证明业务语义正确。

## 首次完整实验暴露的入口问题

`d29a08e7` 的第一次完整实验在进入组级语义抽取前主动中止。它已扫描完整输入，模型实际返回 24 次，其中列角色 23 次、关联 Agent 1 次，合计 84,743 个 provider-reported tokens。该次中止记录保留在本地 `interruption.json`，不作为成功抽取结果。

这次发现两个原有门禁相互影响：每种语义角色最多保留两个字段，物理名称会挤掉中文业务名；模型又把部分唯一编码误列为名称或别名，导致定义模式数随记录编码增长。后续修复保留全部合格语义字段，将无名称依据的编码角色单独记录冲突并保留为关联标识。期间、预算、预测等口径差异继续保留，不能为节省请求而合并。

结构化 JSON 或 schema 返回错误支持一次格式修复，仍使用 `tool_choice=auto` 并消耗共享 800 次预算。批量请求按真实提示词、schema 和证据字节计算；审查请求因加入候选结果而变大时，会单独重新分批，不截断证据。旧的 580 模式、336 次理想调用估计仅适用于未启用角色推断的预检，不能套用到修复后的完整实验。
