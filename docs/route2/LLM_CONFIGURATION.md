# LLM 接口、流式输出与思考模式

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
