# V13 模拟输入数据

这里保存 V13 工作区使用的 `fruit_data_v3` 完整输入：23 张表、1,102,238 行，93 个原始输入文件共 216,418,505 字节。

- `data/`：23 个 CSV。
- `schema/tables/`：23 份表结构。
- `schema/constraints/`：23 份约束说明。
- `schema/foreign_keys/`：23 份外键说明。
- `synthetic_manifest.yaml`：逐表行数、生成来源与输入版本。
- `SHA256SUMS`：上述文件和本说明的 SHA256。

本快照与 `experiments/fruit-full-v10-20260929/synthetic-data.tar.gz` 解压出的 93 个输入文件逐项一致，现展开保存，便于下载、浏览和直接使用。原文件字节与换行保留，未重新生成数据。

数据由 `route2/scripts/generate_fruit_data.py` 和 `generate_fruit_semantic_data.py` 按固定规则生成，不读取真实业务输入、不调用模型。连接信息使用不可登录的合成值。本数据只用于功能和回归验证，不是本体抽取准确性的标准答案。第 23 张表 `fruit_import_staging_temp` 是生成器额外假设的模拟表。

`semantic_fixture_version` 为 3，相比 v2 调整了公式符号、度量定义、指标绑定等输入。因此，使用不同输入版本得到的抽取差异不能全部归因于算法变化。

## 校验

在本目录中执行：

```bash
shasum -a 256 -c SHA256SUMS
```

安装项目依赖后，在 `route2` 目录中执行输入结构及引用一致性校验：

```bash
python scripts/generate_fruit_semantic_data.py validate --input local_data/fruit_data_v3
```

该命令只读取输入，不运行模型抽取。现有运行配置如果指向 `fruit_data_v2`，应显式选择本目录作为输入，不混用两个版本。

如需重新生成，在 `route2` 目录中选择一个新的输出目录：

```bash
python scripts/generate_fruit_semantic_data.py generate --output local_data/fruit_data_v3_rebuilt
```
