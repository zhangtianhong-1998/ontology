# 路线2实现范围与运行说明

本文中的代码、配置路径及命令均以 `prototypes/route2/` 为基准。

代码位于 [code/ontology_r2](../../route2/code/ontology_r2)。本轮按上游 RIGOR 的直接映射、逐表增量、当前 core 上下文、Judge 修正及合并校验逐项改造，详细差异见 [RIGOR 对照](RIGOR_COMPARISON.md)。上游源码保留原样，内部仍采用用户定义的 YAML 模型。

实际流程：导入与分组字段统计 → 有界字段候选及精确核验 → 全表全字段直接映射 → 按声明依赖遍历工作单元 → 候选证据、原生 ReAct/MCP 知识和外部模型对齐 → 生成 delta → 候选合并与程序校验 → Judge/修正复核 → 接受增量或保留旧 core → 批量记录抽取 → 本地结果页面。候选统计不是已接受的业务关系。

增量构建只遍历一次表目录，每张表对应一个工作单元；23 张表就是 23 个单元，不存在反复遍历全图直至收敛的外层循环。每表最多尝试 `llm.max_repairs + 1` 次；两个真实模型配置 `runtime.real.example.yaml`、`runtime.no-thinking.yaml` 都设为 `max_repairs: 4`，即每表最多 5 次。每次先生成并校验增量，校验通过后交给 Judge；成功后立即进入下一表，失败则保留原 core，不会自动回访。`manifest.yaml` 和 `construction.yaml` 的 `iteration_policy`、`steps[*].attempts` 记录配置与实际尝试。

两个真实模型配置的共享调用上限为 800 次。若 23 张表都用满 5 次且每次都进入 Judge，仅生成与 Judge 最多需 230 次；成功提前结束时调用数更少。MCP ReAct 检索轮数是另一层预算，当前默认最多 3 轮、可配置至 5 轮；其调用与生成、Judge、外部对齐和文本关联共用这 800 次。`max_reserved_tokens: 90000000` 避免旧的 200 万预留额度先于 800 次调用耗尽；预留按请求字节数加输出上限计算，并非实际 token 用量或费用。模拟配置仍保持较小预算。

运行时默认向 **stderr** 输出导入 CSV、字段统计、候选召回及核验、增量工作单元、对象物化、关系记录、按需构建文本索引、YAML 分片、结果校验和页面生成进度；stdout 保留最终 JSON。终端显示单行进度条，日志/管道环境按阶段或里程碑输出，避免逐行刷屏。可在 YAML 中设置 `progress.enabled: false`，或用 `build --no-progress` 关闭；`progress.min_interval_seconds` 控制终端刷新间隔。记录进度的总量以本次配置的记录上限为界，显示 100% 不代表预算外记录也已处理，未处理范围仍以 `manifest.yaml` 和 `coverage.yaml` 为准。

企业检索使用 **AgentScope 2.0.8 的 `Agent` + `ReActConfig`**。该版本已没有 `ReActAgent` 类名；当前使用框架原生的工具调用循环，模型自主决定查询、读取、改写与收尾。仅注册两个只读 MCP 工具，并共享本次运行的模型预算。

RIGOR 原版以 SentenceTransformer/FAISS 检索企业文档、外部本体和累积 core。路线2已补上三个可控的本地向量环节：外部本体卡以 FTS + 精确余弦取候选并集，按倒数排名融合排序；已接受表的表注释、字段注释和对象类型组成文本卡，相似表仅作为后续单元的上下文；企业 MCP 已返回的标题/片段做本地余弦重排。客户端未给企业文档建立全量本地向量索引，实际服务端的召回方式须另行确认。配置见 [LLM 与 embedding](LLM_CONFIGURATION.md)，验证边界见 [STATUS.md](STATUS.md)。

## 1. 实际实现

- 三目录 YAML 和 CSV 导入；原值保留为字符串，保存文件哈希、原始约束、字段说明和快照记录位置。字段统计按最多 32 列分组，区分 CSV 空字段、空字符串、空白及可用值，近似基数和样本范围明确标记。
- 有界字段候选发现：声明外键、字段名、有限原值倒排取并集；对选中字段对精确报告匹配、目标重复和缺失，并把少量已核验摘要加入逐表 LLM 上下文。未检查范围写入 `discovery_coverage.yaml`；不自动把值重叠编译为关系。
- 元数据图、全字段直接映射和根模型约束；逐表生成增量、校验临时合并结果、接受或回滚。失败单元仍保留直接映射并标记 partial。
- 按条件执行的引用匹配；目标索引含作用域，歧义保留，空条件采用三值逻辑。
- 有限公式语法及符号原文位置、JSON 键路径、字符串/JSON 成员列表、SQLite FTS 文本候选与有预算的模型判断。显式允许成员单独输出规则；观察样本不会自动成为允许域。
- AgentScope 2.0.8 结构化模型接口；调用、token 预留、输入大小与修复次数上限；原始提示输入、可见结构化响应和异常轨迹。
- 真实 stdio MCP 客户端及本地模拟服务；原生 ReAct Agent 自主查询与读取，默认 3 轮、上限 5 轮，另有 1 次结构化收尾。开启 embedding 后，仅对 MCP 已返回的标题/片段做本地余弦重排，保留原始名次；每条知识仍校验原文、范围和当前工作单元的源证据 ID；无知识返回空列表。
- RDF/CDM 本地参考卡导入；以内部术语生成译词/别名查询，再检查候选定义和原文引用。内部 ID 保持不变。导入器只解释已声明子集，保留原文件。
- 可选本地 embedding：外部本体卡 FTS5 + 精确余弦召回并集，RRF 排序并记录来源/分数；已接受表的语义邻居只补充当前 core 上下文。模型按本地目录加载，记录文件哈希、维度及编码数量；模型不存在或超出卡片上限时失败，不静默降级。
- SQLite 工作存储、YAML 分片、按计划统计覆盖率、未决项、结构引用检查和运行 manifest。调用预算、语义组预算或记录上限耗尽时标为 `partial`。

- 本地 `viewer.html`：实例关联、本体类型、元数据、构建增量、补充知识和未决项。点击节点或关系查看证据；默认最多预览 200 个节点。

## 2. 安装与模拟运行

要求 Python 3.11+。以下命令在本目录执行。Windows 11 / Python 3.14.7 无 Docker 的离线安装另见[专项说明](DATAHUB_OFFLINE.md)：

```bash
uv sync --locked --extra test
uv run ontology-r2 build --config config/runtime.mock.yaml --output runs/my-mock-001
uv run pytest -q
```

`fixtures/linked` 已提供，可以直接运行。已存在的结果目录不会覆盖，重跑时换一个输出名称。模拟数据和手写模拟模型响应均明确标记，不代表真实业务中存在这些字段或关系。

生成其他模拟输入用 `uv run ontology-r2 make-demo --output fixtures/my-data --rows 10000 --scenario linked`；还可选 `unrelated` 或 `formula`。新夹具配套的模型响应和 MCP 文档路径也应同步修改。规模脚本会自动完成这些设置。

## 3. 配置真实 LLM

流式、非流式、思考模式和本地 embedding 的配置见 [LLM_CONFIGURATION.md](LLM_CONFIGURATION.md)。使用 `config/runtime.no-thinking.yaml` 可启动显式关闭思考的真实模型流程；服务参数和环境变量覆盖规则见该文档。本地向量模型目录在 `.env` 的 `ONTOLOGY_EMBEDDING_MODEL_PATH` 中配置，Windows 离线运行前须复制模型文件。


本目录已预留 `.env`，填写 `ONTOLOGY_LLM_MODEL`、`ONTOLOGY_LLM_BASE_URL`、`ONTOLOGY_LLM_API_KEY`，变量格式见 `.env.example`。模型服务需兼容 Chat Completions 的结构化工具输出。

```bash
uv run ontology-r2 build --config config/runtime.mock.yaml --llm-mode agentscope --output runs/llm-001
```

这个命令保留模拟数据和模拟 MCP，只将模型调用替换成真实服务。`--dataset /绝对路径/数据目录` 可换成真实输入。真实数据不应沿用 `synthetic: true` 的标记：复制配置为自己的运行配置，并改为 `synthetic: false`。

`--profile E2L/E2O/E2K/E2F` 分别选择本地、加外部本体、加 MCP、两者都加。例如增加 `--profile E2F` 可同时试用仓库内的 gist 和模拟 MCP。运行 manifest 保存实际开关。`external.sources` 选择参与实验的模型；仓库已提供 gist、Valueflows 和精选 CDM，KPIOnto 需从上游直接取得，路径见 `ontologies/sources.yaml`。无需每次全量加载。

接企业 MCP 时，将 `mock_documents` 去掉，配置 `command`、`args`、`search_tool` 和 `fetch_tool`。首版适配 `search(query, limit) -> {hits:[...]}`、`fetch(document_id) -> {id,text,scope,version}`；不同参数形式需要一个薄适配器。当前只实现 stdio 传输。

## 4. 输出与规模检查

每次独立运行保存 `direct_mapping.yaml`、`construction.yaml`、`incremental/step-*.yaml`、`viewer.html` 和 `ontology.yaml`、`extraction_plan.yaml`、`meta_graph.yaml`、`profiles.yaml`、`coverage.yaml`、`knowledge.yaml`、`alignments.yaml`、`validation.yaml`、`metrics.yaml` 和 `manifest.yaml`。实例、断言、规则、证据和未决项按目录分片；某类结果为空时不生成其分片。

`trace.jsonl` 保存可见的模型输入/结构化输出、缓存命中、MCP 请求/返回和异常。模型密钥来自环境变量，不写入这些记录。`complete` 仅表示本次计划执行完毕；关系未决和语义完整性另行报告。

```bash
# 一万条完整模拟流程
uv run python scripts/benchmark.py --output runs/my-scale-10000 --rows 10000 --record-cap 10000
# 百万条导入，最多执行一万条关系任务；剩余范围必须明确报告
uv run python scripts/benchmark.py --output runs/my-scale-million --rows 1000000 --record-cap 10000
# 独特描述的语义判断预算检查，模拟模型不会替代实际语义评测
uv run python scripts/benchmark.py --output runs/my-scale-text --rows 1000000 --record-cap 1000 --kind text
```

## 5. 明确的实现边界

- 每个增量使用当前表及声明邻居的元数据、最多三行示例、全表字段目录、已有类型定义及选定的 core 邻域。没有外键时按表名稳定遍历；循环依赖单独记录。完整单元超过预算时保留直接映射并标为 partial。复杂表的自动分组与稀有模式采样仍需补充，不能声称覆盖所有隐含关系。
- 字段候选与单列精确核验已有首版，但只对预算内字段/候选执行。精确频次、稀有/反例采样、结构化引用、文本候选、自动条件/复合键提案和安全计划编译仍待实现；见[状态表](STATUS.md)及[详细设计](FIELD_PROFILING_DESIGN.md)。
- 本轮已采用逐表 delta，并保留全局程序校验。YAML 根模型、按计划确定的关系端点及声明约束替代 OWL 结构；没有引入 OWL 推理，也没有把 SQL UNIQUE 解释成非空要求。
- 记录身份限定在来源快照，尚无跨快照稳定业务身份和全局融合。路线3的功能没有提前宣称完成。
- 成员提取支持已有字符串/JSON 列表和 JSON 键路径。允许规则要求显式配置及来源说明，无法只凭观察值建立。自由文本中的复杂范围、否定条件、JSON 通配符或任意公式语法尚不支持；会保留原文和未决原因。
- Embedding 覆盖外部本体卡召回、已接受表的上下文和 MCP 已返回片段的重排；不对企业文档全集或百万行记录建向量索引。MCP 未召回的文档不会因本地重排出现；候选召回和相似度也不能证明本体对齐正确。外部 CDM 仅提取实体及直接父引用，复杂 traits/属性组未解释。
- 字节数加输出上限用于保守预留预算；provider token 数另记。模拟调用与真实调用分开统计。结构检查不等于语义正确性。
- 本次运行结果以验证报告为准；百万条完整端到端性能不能由小样本测试代替。
- 当前每个表计划指定一个基本对象类型；没有跨表身份合并或同表多类型条件分派。没有充分依据时可以选择一般对象或保留未建模状态，不强制归入指标/维度。

代码保持普通函数、小型数据模型和一个企业检索 Agent。生成、Judge、外部对齐使用各自的任务提示；ReAct 检索使用独立 system prompt。`max_rounds` 限制原生迭代，`max_units` 限制企业检索工作单元数，模型调用和 MCP 工具调用另设硬上限。

需求与代码对应关系见 [implementation.md](implementation.md)，实测记录见 [VALIDATION_REPORT.md](VALIDATION_REPORT.md)。

## 6. 查看抽取结果

构建结束后直接打开输出目录中的 `viewer.html`，无需安装前端或上传数据。为已有运行重新生成页面：

```bash
uv run ontology-r2 visualize --run runs/my-mock-001 --max-nodes 200
```

页面显示完整结果数量和当前预览数量；默认只预览部分节点与关系。完整结果仍在 YAML 分片和 SQLite 中。浏览页面不触发新的 LLM 调用。
