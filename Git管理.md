# 获取和维护三个路线

本分支只包含代码、需求设计和运行所需的契约、配置示例。独立提交历史从代码包开始；原研究资料保留在 `main` 分支。当前默认分支为 `codex/three-routes`。

## 获取代码

```bash
git clone --branch codex/three-routes https://github.com/zhangtianhong-1998/ontology.git
cd ontology
git -c core.precomposeunicode=false submodule update --init --recursive
```

9 个子模块固定了 RIGOR、AgentScope，以及仅用于路线3的 SAND、GRAMS、steiner-tree。子模块保留各自的许可证和来源；GitHub 的 ZIP 下载不包含子模块正文，应使用上述命令获取。`core.precomposeunicode` 用于兼容 RIGOR 的 Unicode 文件名。

## 运行路线2

```bash
cd 路线2
uv sync --locked --extra test
uv run ontology-r2 make-demo --output fixtures/linked --rows 8 --scenario linked
uv run ontology-r2 build --config config/runtime.mock.yaml --output runs/my-mock-001
uv run pytest -q
```

默认流程使用模拟模型和模拟 MCP，不需要外部本体或 LLM 密钥。查看结果时打开 `runs/my-mock-001/viewer.html`。真实模型配置见 [运行说明](路线2/IMPLEMENTED.md)。

## 按需取得外部模型

外部本体文件未打包。每个路线的 `ontologies/sources.yaml` 记录预期路径，`resources.yaml` 记录官方来源及版本。启用 E2O / E2F 前先准备所选模型。默认示例使用 KPIOnto，可在路线2目录执行：

```bash
mkdir -p ontologies/KPIOnto
curl --fail --location https://raw.githubusercontent.com/KDMG/kpionto/1c36644a40123447fb6469b9832865b4b4c2f7ba/kpionto.ttl --output ontologies/KPIOnto/kpionto.ttl
printf '%s\n' '1cb6a3a81ecaeb76d2ef592181d80339ce98c09bac60f8558e8f37a0b7cebef2  ontologies/KPIOnto/kpionto.ttl' | shasum -a 256 -c -
```

其他模型按清单下载到对应路线目录，保留其许可证。代码包不承诺已取得这些文件；不要以存在版本清单代替资源完整性检查。

## 后续提交

本仓库直接管理自研代码、测试、内部模型和设计文档。上游源码用子模块提交号管理，不复制其 Git 历史到主仓库。修改自研代码后正常提交；更改子模块版本时一并更新版本清单。

`.env`、私有配置、虚拟环境、模拟或真实数据、外部本体和运行结果均被忽略。`.env.example` 是空值模板。原工作区中的数据和结果保留在本地。
