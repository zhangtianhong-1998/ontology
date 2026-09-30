# v11 全量实验：运行中的结果快照

**本次发布时实验尚未结束。这里保存的是阶段结果，不是最终抽取报告。** 发布没有停止后台进程，也没有重新调用模型。

实验代码提交为 `ff8ded2487307a430d6faed50f8f44f688a6c29e`，位于 `codex/generalized-extraction-v11` 分支。代码在 [`route2/code/ontology_r2`](../../route2/code/ontology_r2/)，改动、验证和后续待办见 [v11 说明](../../docs/route2/GENERALIZATION_V11.md)。启动前 1,152 项测试通过；测试通过不代表全量本体的语义质量已经通过验收。

## 发布范围

- [extraction-checkpoint.tar.gz](extraction-checkpoint.tar.gz)：24 个已落盘文件，包括字段统计、列角色、关联候选与验证、元数据图、DataHub 导出、证据包、语义检查点及完整行截止的调用记录。
- [checkpoint_plan.yaml](checkpoint_plan.yaml)：从本次 `semantic_state.json` 原样导出的当前计划，方便直接检查。它不是最终本体。
- [checkpoint_summary.json](checkpoint_summary.json)：该固定检查点的数量和状态，不代表全量覆盖率。
- [api_call_audit.json](api_call_audit.json)：固定 trace 前缀的请求尝试、响应和 token 统计；存在在途请求，不应当作最终调用总数。
- [snapshot_metadata.json](snapshot_metadata.json)：捕获时间、来源提交、输入身份、逐文件 SHA-256、尚未生成的最终文件和排除范围。
- [runtime.full-v11.yaml](runtime.full-v11.yaml)：原始运行配置；相对路径按 `route2/config/` 位置解释。上限为 3,000 次请求、5 轮迭代，调用间隔 0.2 秒。
- [checksums.json](checksums.json)：发布文件校验值。

检查点中的 `semantic_state.json` 由程序原子替换保存。其余文件分别来自各处理阶段，trace 按完整行截取；本归档不是整个运行目录在同一瞬间的事务快照。读取当前接受的类型和关系时，以检查点中的 `plan` 及证据为准；归档中的 `ontology.yaml`、`extraction_plan.yaml` 可能是较早阶段导出，不能视为最终结果。

捕获时尚未生成 `manifest.yaml`、`metrics.yaml`、`process_exit.json`、`experiment_snapshot.json` 和 `viewer.html`。因此本次不提供最终 HTML 或实验完成结论，也没有伪造这些文件。最终实例化、关系提升、定向修复等结果应在实验结束后另行发布和分析。

## 模拟输入与恢复

输入沿用 v10 的 `fruit_data_v3`，共 23 张表、1,102,238 行，全部为程序生成的模拟数据。92 个 CSV/schema 文件已与当前元数据图中的哈希核对。无需重复下载新的输入包，使用同分支已有的 [v10 模拟数据归档](../fruit-full-v10-20260929/synthetic-data.tar.gz)；其哈希记录在本目录的快照说明中。

```sh
git clone --branch codex/generalized-extraction-v11 https://github.com/zhangtianhong-1998/ontology.git
cd ontology
python -m tarfile -e experiments/fruit-full-v10-20260929/synthetic-data.tar.gz route2/local_data
python -m tarfile -e experiments/generalized-full-v11-20260930/extraction-checkpoint.tar.gz route2/runs
```

请解压到空目标目录，避免覆盖自己的运行数据。查看 YAML/JSON 不需要模型服务。重新运行需要自行按 [环境变量示例](../../route2/.env.example) 配置模型及本地 embedding，并使用新的输出目录。

归档排除了所有运行中的数据库、临时文件、进程标准输出日志、`.env`、密钥、模型权重、Python 环境和 LLM 缓存。输入中的连接字段是生成器写入的测试值；没有真实企业数据。发布文件保留原始来源标识和本地路径用于追溯，且已完成配置密钥及常见凭证格式扫描。
