# LLM 接口、思考模式与本地向量检索

本文中的代码、配置路径及命令均以仓库的 `route2/` 目录为基准；文档链接可直接点击。

配置对增量生成、Judge、关系判定、外部对齐及 ReAct 检索统一生效。实现见 `code/ontology_r2/transport.py`、`llm.py` 和 `knowledge.py`。

## 配置

```yaml
llm:
  mode: agentscope
  stream: true
  thinking:
    mode: disabled
    parameter: chat_template_kwargs
    effort: medium
  temperature: 0
  timeout_seconds: 60
```

- `stream: true` 使用 SSE 流式接口，`false` 使用普通 JSON 响应。两者都要求服务支持结构化工具调用。
- `thinking.mode` 可取 `disabled`、`enabled`、`provider_default`。前两者显式发送控制参数；`provider_default` 不发送思考开关，采用服务端默认值。
- `thinking.parameter` 必须与服务的接口约定一致，不能按模型名称猜测。
- `thinking.effort` 仅用于 `enabled + reasoning_effort`。某些模型不接受 temperature；可配置为 `null`，此时不发送该参数。
- 增量生成、Judge 等结构化请求只提供 `submit_result` 工具，并发送 `tool_choice: auto`，兼容不接受指定工具模式的服务。返回时仍要求恰好一次 `submit_result` 调用且参数符合 schema；纯文本或其他工具调用会报错，不写入缓存。企业检索的 ReAct 工具循环由 AgentScope 单独管理。

真实模型运行配置 `runtime.real.example.yaml` 与 `runtime.no-thinking.yaml` 均设 `llm.max_calls: 800`、`llm.max_repairs: 4`。后者表示每张表最多 5 次生成与校验尝试，接受后立即停止，并非全库遍历 5 轮。生成、Judge、检索、对齐及文本判定共用 800 次调用；缓存命中不消耗调用次数。`llm.max_reserved_tokens: 90000000` 是按每次请求 UTF-8 字节数加输出上限累计的准入预算，可容纳 800 次达到 `max_input_bytes: 100000` 的请求；不代表实际 token 消耗。若需把企业 MCP 的 ReAct 检索也改为最多 5 轮，另设 `mcp.max_rounds: 5`，它不受 `llm.max_repairs` 控制。

| parameter | disabled 时发送的请求字段 | 典型适用接口 |
|---|---|---|
| `chat_template_kwargs` | `{"chat_template_kwargs":{"enable_thinking":false}}` | Qwen 的 vLLM 模板控制 |
| `enable_thinking` | `{"enable_thinking":false}` | 支持该开关的百炼兼容接口 |
| `thinking` | `{"thinking":{"type":"disabled"}}` | 支持该字段的 DeepSeek 接口 |
| `reasoning_effort` | `{"reasoning_effort":"none"}` | 明确支持 `none` 的兼容接口 |

启用时分别发送 `true`、`true`、`type: enabled` 或选定的 effort。具体模型必须支持对应值；有些思考模型不能关闭思考。服务拒绝参数时保留错误，不自动删除参数重试，也不把隐藏输出当成关闭思考。

接口依据：[Qwen 工具调用](https://qwen.readthedocs.io/en/latest/framework/function_call.html)、[百炼思考模式](https://www.alibabacloud.com/help/zh/model-studio/deep-thinking)、[DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。`reasoning_effort` 的可用值仍以所选模型说明为准。

## 环境变量

先执行 `cp -n env.example .env`，填写模型、地址和密钥。`.env.example` 内容相同，保留原使用方式。

| 环境变量 | 配置字段 |
|---|---|
| `ONTOLOGY_LLM_STREAM` | `llm.stream`，`true` 或 `false` |
| `ONTOLOGY_LLM_THINKING_MODE` | `llm.thinking.mode` |
| `ONTOLOGY_LLM_THINKING_PARAMETER` | `llm.thinking.parameter` |
| `ONTOLOGY_LLM_REASONING_EFFORT` | `llm.thinking.effort` |

优先级：进程环境变量优先于 `.env`；非空环境变量覆盖 YAML，空值沿用 YAML。代码未配置思考模式时保持 `provider_default` 以兼容旧配置；新配置示例明确设置 `disabled`。`manifest.yaml` 记录本次有效配置，缓存也按有效配置和服务地址哈希区分。

## 启动关闭思考的模式

在 `route2` 目录执行：

```bash
uv sync --locked --extra test
cp -n env.example .env
# 代码包若尚无模拟数据，先生成；已有 fixtures/linked 时跳过此条。
uv run ontology-r2 make-demo --output fixtures/linked --rows 8 --scenario linked
uv run ontology-r2 build --config config/runtime.no-thinking.yaml --output runs/no-thinking-001
```

`runtime.no-thinking.yaml` 使用真实模型、流式接口和 `disabled`，输入及 MCP 仍为模拟环境。运行前按服务选择 `thinking.parameter`。需要普通接口时将该文件的 `llm.stream` 改为 `false`，或设置 `ONTOLOGY_LLM_STREAM=false`。

## 完整性与验证

SDK 负责拼接工具调用的分片。主程序在收到完整响应后才解析 JSON、校验模型和写入缓存、本体。断流、超时、`length` / `content_filter` 终止和损坏 JSON 都会失败，已接受的本体增量按原流程保留。

一次接口调用包含连接和消费完整响应的统一超时；token 用量只统计最终累计值一次。流式过程将可见工具参数写入 `llm_stream_delta` 事件，最终结果继续保留完整结构化输出。隐藏的思考字段不写入轨迹；Agent 内需要继续调用工具的消息仍交由 SDK 处理。

`tests/test_transport.py` 用本地 HTTP 服务验证 JSON/SSE、分片拼接、思考开关的实际请求字段、环境变量覆盖、ReAct 共用配置、超时和不完整响应。它验证接口行为，不能证明某个在线模型确实遵从了关闭指令。

## 本地 embedding 配置

Embedding 模型与上面的生成式 LLM 分开配置。先执行 `uv sync --locked --extra embedding` 安装可选依赖，再将 `env.example` 复制为 `.env`。`ONTOLOGY_EMBEDDING_MODEL_PATH` 指向**完整的本地模型目录**；模板写的是用户现有 macOS 路径。Windows 离线机器须先复制模型文件和相应依赖轮子，再把该变量改为本机路径，例如 `C:/models/Qwen3-Embedding-0.6B`。`ONTOLOGY_EMBEDDING_ENABLED=true/false` 可覆盖 YAML 开关，留空则使用 YAML。运行时以本地路径加载，不自动下载模型；未复制模型时保持默认 `enabled: false`，模拟配置也默认关闭。

```yaml
embedding:
  enabled: true
  model_path: null  # 留空时读取 ONTOLOGY_EMBEDDING_MODEL_PATH
  batch_size: 16
  max_cards: 20000
  core_top_k: 5
  min_cosine_similarity: 0.35
  query_prompt: null  # 留空时使用模型自带的 query prompt
```

开启后有三个本地向量环节：外部本体卡分别从 FTS5 和精确余弦召回，取并集后按倒数排名融合排序；逐表构建时，把**已接受表**的表注释、字段注释和对象类型组成文本卡，取相似表作为当前单元上下文；企业 MCP 搜索返回候选后，以标题和片段的余弦相似度重排。外部本体候选记录检索通道、融合分数、余弦相似度及模型哈希；core 邻居记录相似度；MCP 候选保留原始名次。它们均不直接成为已接受的本体关系。

`max_cards` 是外部本体卡的硬上限，超限直接报错，不会静默只索引一部分。`core_top_k` 限制进入当前工作单元的相似表数，不裁剪 core 本身。`min_cosine_similarity` 只过滤外部本体的低分向量候选，默认 0.35 是待业务样本校准的启发式门槛；FTS 候选不受影响。`embedding_report.yaml` 记录模型文件哈希、维度、文档及查询的编码次数。百万行 CSV 不逐行编码，也不为每条记录调用 embedding 或 LLM。Qwen3-Embedding-0.6B 在本机离线加载的 smoke 测试得到 1024 维；“营业收入的定义”对相关收入文本和无关天气文本的余弦分别为 0.7564/0.1215，只验证模型可加载并能区分该测试对，不能证明真实业务检索质量。

企业 MCP 的候选集合仍由服务端决定。客户端 ReAct Agent 控制查询改写、循环检索和摘要；本地重排只调整已返回候选的顺序，无法扩大服务端召回，也无法保证 MCP 本身采用向量搜索。真实企业 MCP 的召回效果仍需单独评估，见 [实现状态](STATUS.md) 和 [测试说明](TESTING.md)。
