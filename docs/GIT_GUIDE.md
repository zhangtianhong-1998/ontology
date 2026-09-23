# 获取和维护三个路线

本分支包含代码、需求设计、运行契约、配置示例、路线2的手写合成夹具，以及带许可的精选公开模型。独立提交历史从代码包开始；原研究资料保留在 `main` 分支。当前默认分支为 `xuanyue`。

## 获取代码

```bash
git clone --branch xuanyue https://github.com/zhangtianhong-1998/ontology.git
cd ontology
git -c core.precomposeunicode=false submodule update --init --recursive
```

9 个子模块固定了 RIGOR、AgentScope，以及仅用于路线3的 SAND、GRAMS、steiner-tree。子模块保留各自的许可证和来源；GitHub 的 ZIP 下载不包含子模块正文，需要研究上游源码时执行上述初始化命令。路线2的本地原型及合成测试不依赖子模块。`core.precomposeunicode` 用于兼容 RIGOR 的 Unicode 文件名。

## 运行路线2

```bash
cd route2
uv sync --locked --extra test
uv run ontology-r2 build --config config/runtime.mock.yaml --output runs/my-mock-001
uv run pytest -q
```

`fixtures/linked` 已随分支提供，默认流程使用模拟模型和模拟 MCP，不需要外部本体或 LLM 密钥。查看结果时打开 `runs/my-mock-001/viewer.html`。无 Docker 的 Windows 离线安装见[专项说明](route2/DATAHUB_OFFLINE.md)。

## 外部模型

gist、Valueflows 和 7 个 CDM 实体文件已分别放在路线1B、2、3的 `ontologies/`，包含来源清单与许可。默认外部模型示例使用 gist，可离线运行。CDM 为精选子集，不是完整模型。完整范围和许可见[公开本体说明](PUBLIC_ONTOLOGIES.md)。

KPIOnto 公开可获取，但上游未写明再分发许可，所以仓库只保存固定来源、哈希和获取脚本；本体正文不随 Git 上传。需要 KPIOnto 时，在仓库根目录显式执行：

```bash
python scripts/fetch_kpionto.py --download
# 或在无网络的目标电脑上，校验并复制预先取得的文件
python scripts/fetch_kpionto.py --source /path/to/kpionto.ttl
```

下载脚本直接访问上游，只在用户显式执行时联网。`ontologies/sources.yaml` 标明每个模型是否已打包；`resources.yaml` 记录官方来源及版本。

## 后续提交

本仓库直接管理自研代码、测试、内部模型和设计文档。上游源码用子模块提交号管理，不复制其 Git 历史到主仓库。修改自研代码后正常提交；更改子模块版本时一并更新版本清单。

`.env`、私有配置、虚拟环境、真实数据、未获再分发许可的 KPIOnto 正文、CDM 全量库和运行结果均被忽略；新生成的模拟数据也默认忽略，只有明确提交的 `fixtures/linked` 合成夹具例外。`.env.example` 是空值模板。原工作区中的数据和结果保留在本地。
