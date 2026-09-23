# 公开参考模型的本地快照

路线1B、2、3各自保存一份可离线读取的参考模型快照。它们只为内部 YAML 本体提供术语候选，不能直接证明企业数据中的对象身份或关系。路线1A不使用外部模型。

| 模型 | 本仓库内容 | 固定来源 | 许可 |
|---|---|---|---|
| gist | `gistCore.ttl` 等 4 个 Turtle 模块、README、LICENSE、校验清单 | [Semantic Arts gist `c73068b`](https://github.com/semanticarts/gist/tree/c73068bfe779db2643b1e43c920cc8039b15a013) | [CC BY 4.0](https://github.com/semanticarts/gist/blob/c73068bfe779db2643b1e43c920cc8039b15a013/LICENSE.txt)，保留署名；自定义术语使用自己的命名空间 |
| Valueflows | `vf.ttl`、README、LICENSE、来源和校验清单 | [Valueflows 规范](https://www.valueflo.ws/specification/spec-overview/)；文件哈希见 `source.yaml` | [CC BY-SA 4.0](https://github.com/valueflows/valueflows/blob/2210a441bbd67fba5adfdeea442b6a85ade9e16e/LICENSE.txt)，改编时遵守同一许可 |
| Microsoft CDM | 7 个实体定义及 LICENSE、LICENSE-CODE、ThirdPartyNotices | [CDM `dd21d71`](https://github.com/microsoft/CDM/tree/dd21d715e05ebf740a11356c80b5c3b4c38a89c2) | 模型和文档 [CC BY 4.0](https://github.com/microsoft/CDM/blob/dd21d715e05ebf740a11356c80b5c3b4c38a89c2/LICENSE)，代码 MIT |
| KPIOnto | 仅来源、版本与 SHA-256；**不含本体正文** | [KPIOnto `1c36644`](https://github.com/KDMG/kpionto/tree/1c36644a40123447fb6469b9832865b4b4c2f7ba) | 未发现明确的再分发许可 |

CDM 只选 `Account`、`Address`、`Contact`、`Currency`、`Organization`、`Product`、`Invoice` 的源文件，保留原目录层级。`checksums.yaml` 列出准确路径和哈希。它们不是完整的 CDM，也没有包含全部 `imports` 依赖；当前适配器只读取实体名称、描述和父实体引用。

KPIOnto 的仓库公开可读，但[上游 README](https://github.com/KDMG/kpionto/blob/1c36644a40123447fb6469b9832865b4b4c2f7ba/README.md)和 [TTL](https://github.com/KDMG/kpionto/blob/1c36644a40123447fb6469b9832865b4b4c2f7ba/kpionto.ttl)没有写明许可。公开仓库不自动授予再分发权，见 [GitHub 许可说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)。因此本仓库不复制其本体文件。需要在本地实验时，可从上游直接取得并校验：

```bash
python scripts/fetch_kpionto.py --download
# 无网络的目标电脑：先合法取得文件并拷贝过去，再执行
python scripts/fetch_kpionto.py --source /path/to/kpionto.ttl
```

命令将校验 SHA-256 `1cb6a3a81ecaeb76d2ef592181d80339ce98c09bac60f8558e8f37a0b7cebef2`，再写入路线1B、2、3的本地目录。默认配置已改用仓库内的 gist，离线基础实验无需 KPIOnto。
