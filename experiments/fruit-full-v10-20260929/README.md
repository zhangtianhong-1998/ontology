# 水果模拟数据全量输入实验 v10

本目录保存已结束的 v10 实验及对应模拟输入。运行状态为 **partial**，进程退出码为 0；结果仍有未覆盖和未决项，不能据此认定业务本体质量通过验收。本次发布没有重新调用模型或修改原实验结果。

实验代码提交：`cf6ba4f46400a39336bd53536c7050608fc239d3`。发布分支 `codex/fruit-full-v10-results` 保留该实现，新增归档与说明。代码位于 [`route2/code/ontology_r2/`](../../route2/code/ontology_r2/)，设计变更见[迭代复用与修复说明](../../docs/route2/ITERATIVE_REUSE_REPAIR.md)。

## API 调用统计

| 范围 | 请求尝试 | 收到服务端响应 |
| --- | ---: | ---: |
| v10 全量运行 | 491 | 490 |
| 启动前两次小规模预检 | 3 | 3 |
| 本轮 v10 合计 | 494 | 493 |

全量运行的 491 次由 **472 个初始逻辑请求、18 次格式修复、1 次连接错误重试**组成，缓存命中为 0。490 个响应中有 10 次因输出长度限制被拒绝；响应数量不等于有效抽取数量。那次连接失败是否抵达服务端或计费，无法从本地日志确认。

全量运行的服务端 token 统计为 **9,368,402**。`reserved_token_upper_bound` 是本地预算上界，不能作为实际消耗。详细计数与任务分项见 [api_call_audit.json](api_call_audit.json)；原始 `trace.jsonl` 在结果归档中。

## 文件内容

| 文件 | 内容 |
| --- | --- |
| [viewer.html](viewer.html) | 原实验生成的 HTML，下载后直接打开 |
| [synthetic-data.tar.gz](synthetic-data.tar.gz) | `fruit_data_v3`：23 个 CSV、69 个 schema YAML、模拟数据说明 |
| [extraction-results.tar.gz](extraction-results.tar.gz) | 全部导出结果、实例节点与关系、证据、未决项、trace、语义状态、HTML，以及 `work/results.sqlite` |
| [run_manifest.yaml](run_manifest.yaml) | 原始运行清单、调用统计及未完成原因 |
| [metrics.yaml](metrics.yaml) | 原始结果指标 |
| [ontology_instance_coverage.yaml](ontology_instance_coverage.yaml) | 实例化范围、未决数量与未实例化类型 |
| [targeted_repair_rounds.yaml](targeted_repair_rounds.yaml) | 第二轮定向修复记录 |
| [runtime.fruit-templates.yaml](runtime.fruit-templates.yaml) | 原始运行配置；相对路径按原 `route2/config/` 位置解释 |
| [checksums.json](checksums.json) | 归档文件、逐项内容的 SHA-256、代码版本与打包范围 |

输入为 **23 表、1,102,238 行**程序生成的数据，不含真实企业数据。92 个 CSV/schema 来源文件已逐项与本次元数据图中的哈希核对。模拟输入版本为 v3，与 v8 归档的 v2 不同；不能把二者的输出差异全部归因于算法修改。

本次耗时约 **3 小时 36 分钟**，北京时间 2026-09-29 05:09 结束。原程序记录的结果包括 39 个业务关系类型、17,522 个定义/配置记录实例、55,978 条实例断言；经营观测实例为 0。实例化仍有 5,736 项未决，另有 1,600 个事实候选未实例化。数量是程序产物统计，不代表语义准确率。

第二轮调度已执行，但模型调用为 0，以 `no_semantic_change` 停止。预算上限为 3000 次，本次未耗尽；`partial` 的具体原因保留在运行清单中，不能全部解释为调用预算不足。

## 下载与恢复

```sh
git clone --branch codex/fruit-full-v10-results https://github.com/zhangtianhong-1998/ontology.git
cd ontology
python -m tarfile -e experiments/fruit-full-v10-20260929/synthetic-data.tar.gz route2/local_data
python -m tarfile -e experiments/fruit-full-v10-20260929/extraction-results.tar.gz route2/runs
```

恢复目录分别为 `route2/local_data/fruit_data_v3/` 和 `route2/runs/fruit-templates-full-v10-20260929/`。请使用空目标目录，避免覆盖自己的文件。

查看已有结果不需要模型、Docker 或 DataHub 服务。GitHub 不直接执行仓库 HTML，需下载本目录的 `viewer.html` 到本地打开。重新抽取时，按 [环境变量示例](../../route2/.env.example) 配置模型服务和本地 embedding，使用 `route2/config/runtime.fruit-templates.yaml` 并指定新输出目录。模拟生成器为 [`generate_fruit_semantic_data.py`](../../route2/scripts/generate_fruit_semantic_data.py)。

归档保留原始本地路径、来源标识和未决状态。排除 `.env`、密钥、模型权重、Python 环境、LLM 缓存，以及可从 CSV 重建的语义索引和 DuckDB；这不是运行环境快照。数据中的连接信息是生成器写入的测试值，不能连接真实服务。
