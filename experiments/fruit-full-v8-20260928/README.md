# 水果模拟数据全量输入实验 v8

这是一次已结束的实验归档，状态为 **partial**，不表示本体质量验收通过。本次上传仅打包已有结果、对应代码和模拟数据，没有重新抽取、应用后续补丁或开展统一语义分析。

## 包含内容

| 文件 | 内容 |
| --- | --- |
| [viewer.html](viewer.html) | 原实验生成的完整 HTML，下载后直接用浏览器打开 |
| [synthetic-data.tar.gz](synthetic-data.tar.gz) | 全部 23 个 CSV、69 个 schema YAML 和合成数据说明 |
| [extraction-results.tar.gz](extraction-results.tar.gz) | 完整导出结果、证据、覆盖报告、trace、状态文件、HTML，以及 `work/results.sqlite` |
| [run_manifest.yaml](run_manifest.yaml) | 原始运行清单，包括状态、配置摘要、调用统计和未完成原因 |
| [metrics.yaml](metrics.yaml) | 原始运行指标 |
| [runtime.fruit-full.yaml](runtime.fruit-full.yaml) | 运行时配置原件；在原来的 `route2/config/` 位置使用 |
| [checksums.json](checksums.json) | 归档文件及逐项内容的 SHA-256、源代码提交与打包范围 |

代码位于仓库中的 `route2/code/ontology_r2/`，实验源版本为 `261f9ecc1d10f250df4f0e67db2abbb5542e525d`。本发布分支保留该实现，仅新增归档和更新说明；本机尚未应用的隔离补丁没有混入此版本。模拟数据生成器为 [`generate_fruit_data.py`](../../route2/scripts/generate_fruit_data.py)。

## 运行记录

- 输入：23 张表、1,102,238 行，全部为程序生成的水果领域模拟数据，不含真实企业数据。
- 状态：`partial`；进程正常退出，耗时约 4 小时 40 分钟。
- 模型：`deepseek-v4-flash`，800 次调用，服务端报告 18,747,517 tokens；另有 23 次精确缓存命中。
- 2,007 个证据包中，1,275 个完成判断：534 个接受、734 个未决、7 个无新增；下一包触发预算耗尽。
- 已接受数量是程序当时的判定结果，不能当作语义准确率。统一结果分析尚未进行。

原始产物保留了实验时的本地路径、来源标识和未决状态，没有为上传修改语义内容。完整未处理原因见运行清单。

## 下载与查看

克隆本分支后，可直接打开本目录的 `viewer.html`。GitHub 不直接执行仓库中的 HTML，需要下载到本地打开。

在仓库根目录执行以下命令，可将输入与结果恢复到默认位置；适用于已安装 Python 的 Windows、macOS 和 Linux：

```sh
python -m tarfile -e experiments/fruit-full-v8-20260928/synthetic-data.tar.gz route2/local_data
python -m tarfile -e experiments/fruit-full-v8-20260928/extraction-results.tar.gz route2/runs
```

输入恢复为 `route2/local_data/fruit_data_v2/`，结果恢复为 `route2/runs/association-rebuild-full-v8-20260928/`。请使用空的目标目录，避免覆盖自己已有的实验。

查看已有结果不需要模型或 DataHub 服务。若要重新执行抽取，请按 [`route2/.env.example`](../../route2/.env.example) 和[运行说明](../../docs/route2/IMPLEMENTED.md)配置模型服务及本地 embedding 路径，再使用 `route2/config/runtime.fruit-full.yaml`，并指定新的输出目录。重新调用模型可能得到不同结果。

不包含 `.env`、模型权重、LLM 缓存、Python 环境、2.7 GB 语义索引缓存或可由 CSV 重建的 DuckDB 文件。因此这是结果归档，不是可直接恢复原执行进程的运行环境快照。数据中的连接信息是生成器写入的测试值，不能用于真实服务。
