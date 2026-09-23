# Windows 离线运行与 DataHub 文件导出

本文以 Windows 11 x64、CPython 3.14.7、PowerShell 为目标环境，不使用 Docker。以下命令均在路线2工程目录执行：`xuanyue` 分支下载后是 `route2\`，本地研究仓库中是 `prototypes\route2\`。

## 运行方式与功能边界

路线2仍由本地 `meta_graph.yaml` 保存表、列、约束、来源和声明外键，并由现有流程构建本体及记录关联。`export-datahub` 只把**表和列元数据**写成 DataHub metadata-file JSON；它不读取 CSV 记录，不导出实例关系，也不推断血缘。当前导出器直接生成文件，**不依赖 DataHub Python SDK 或服务端**。[DataHub metadata-file 说明](https://github.com/datahub-project/datahub/blob/master/metadata-ingestion/sink_docs/metadata-file.md)

| 组件 | 在本原型中的作用 | 是否需要部署 |
|---|---|---|
| 本地元数据图 | 路线2的技术元数据与来源记录 | 构建时自动生成 |
| DataHub 格式文件 | 可选交换文件，含 `datasetProperties`、`schemaMetadata` | 导出不需安装 DataHub |
| DataHub Python SDK | DataHub 元数据模型及与服务端交互的客户端；本项目导出命令不调用它 | 导出不需安装 |
| DataHub Lite | 基于 SDK 包提供的可嵌入本地元数据存储，可选导入与查询 | 放在独立虚拟环境 |
| 完整 DataHub 服务 | GMS、搜索和图查询等服务端能力 | 本文不部署，未验证原生 Windows 安装 |

完整 DataHub 的[元数据模型](https://github.com/datahub-project/datahub/blob/master/docs/modeling/metadata-model.md)支持关系遍历。[DataHub Lite 官方文档](https://github.com/datahub-project/datahub/blob/master/docs/datahub_lite.md)明确说明 Lite **不支持关系图遍历（包括血缘）和 GraphQL**，所以 Lite 不能替换本地构图算法或完整 Metadata Graph 服务。完整服务的组成见[官方架构说明](https://github.com/datahub-project/datahub/blob/master/docs/architecture/architecture.md)；下载 DataHub 源码或安装 Python 包均不会自动启动完整服务。

本机另有按 SHA-256 核对的 DataHub `v1.7.0.1` 源码副本，放在 `route2/code/DataHub`，只作适配参考。该目录被 Git 忽略，克隆 `xuanyue` 不会带上它；离线路线2运行也不需要它。若要在另一台机器查阅源码，可按 [resources.yaml](../../route2/resources.yaml) 中锁定的 archive URL 和 SHA-256 单独下载或转移。源码版本 `v1.7.0.1` 与可选 Lite 包版本 `1.7.0.12` 分别记录，不能混称为同一版本。

## 1. 准备离线材料

在联网机器直接克隆公开仓库的 `xuanyue` 分支，例如 `git clone --branch xuanyue --single-branch https://github.com/zhangtianhong-1998/ontology.git`，并准备 Windows 11 x64 的 Python 3.14.7 安装包。路线2运行不需要初始化 RIGOR、AgentScope 等参考源码子模块。把代码和离线依赖包复制到目标 Windows 机器。安装脚本的 `-Bundle` 参数指向如下目录：

```text
bundle\
  requirements-route2-locked.txt
  requirements-route2-hashed.txt
  requirements-datahub-lite-locked.txt    # 仅启用 Lite 时
  sha256-manifest.json
  wheelhouse\
    ontology_route2-0.3.0-py3-none-any.whl
    其他 Windows cp314 依赖轮子
```

`wheelhouse`、真实 CSV、`.env` 和运行结果不随 Git 仓库提供。本地准备目录 `.cache\wheelhouse-win-py314\` 已被 Git 忽略，仅克隆仓库不足以离线安装。

目前已按 `win_amd64 / cp314` 分别下载路线2锁定依赖与 `acryl-datahub[datahub-lite]==1.7.0.12` 的轮子，构建了路线2纯 Python wheel，并用 `pip download --no-index` 分别核对了两套依赖闭包。在联网的 macOS 准备机上，可用仓库脚本从 `uv.lock` 导出目标要求、复用已有缓存、补齐轮子并生成带 SHA-256 的 bundle。运行脚本须用 Python 3.11+；`--pip-python` 指向本机带 pip 的 Python，`uv` 命令也须已安装。本机示例如下：

```bash
.venv-datahub/bin/python scripts/prepare_windows_offline.py \
  --bundle .cache/windows-offline-bundle \
  --cache .cache/wheelhouse-win-py314 \
  --pip-python .venv-datahub/bin/python \
  --with-datahub-lite
```

只准备路线2时去掉 `--with-datahub-lite`。脚本先复用本地缓存，缺轮子时再从网络补取。`sha256-manifest.json` 记录目标平台、锁文件哈希、轮子及要求文件的哈希和离线闭包检查结果。将整个 bundle 复制到 Windows；不要只复制 `wheelhouse`。如需在离线机器运行测试，把 `pytest>=8,<10` 及其依赖也加入 wheelhouse。

`pip download` 可以指定目标平台，`pip install --no-index --find-links` 只从本地目录安装；参见 [pip download](https://pip.pypa.io/en/stable/cli/pip_download/) 与 [pip install](https://pip.pypa.io/en/stable/cli/pip_install/) 官方文档。跨平台依赖检查**不等于 Windows 真机安装成功**。两套依赖可放在同一 wheelhouse，**不能合装在同一虚拟环境**：当前解析发现路线2锁定的 `uvicorn==0.53.0` 与 Lite 1.7.0.12 存在版本冲突。

## 2. 安装路线2并跑模拟输入

在离线 Windows 机器确认 `py -3.14 -V` 输出 Python 3.14.7。安装脚本只读本地 bundle，用 `pip check` 检查依赖，并把路线2装入 `.venv`。若计划试 Lite，**首次安装时**在命令末尾加 `-WithDataHubLite`，脚本会另建 `.venv-datahub`；已有 `.venv` 不应再运行安装脚本。[Python Windows 虚拟环境文档](https://docs.python.org/3.14/library/venv.html)

```powershell
py -3.14 -V
& .\scripts\install_windows_offline.ps1 -Bundle C:\path\to\bundle
.\.venv\Scripts\python.exe -m ontology_r2.cli build --config .\config\runtime.mock.yaml --output .\runs\win-mock-001
```

`runtime.mock.yaml` 含合成 CSV、预录模型响应和模拟 MCP，无需启动 LLM。输出目录必须是新目录。结束后检查 `runs\win-mock-001\manifest.yaml`、`validation.yaml` 和 `viewer.html`；`manifest.status` 只表示本次执行状态，不代表业务语义正确。

## 3. 使用本地模型和自己的输入

复制真实运行配置到被 Git 忽略的 `local_config\`，将 `dataset` 指向包含 `data\*.csv`、`schema\tables\*.yaml`、`schema\constraints\*.yaml`、`schema\foreign_keys\*.yaml` 的目录。相对路径以**配置文件所在目录**为基准；检查 `dataset`、`model_profile` 和 `env_file` 的解析位置。`data_scope` 应如实标记完整导出、样本或未知。

```powershell
New-Item -ItemType Directory -Force .\local_config | Out-Null
Copy-Item .\config\runtime.real.example.yaml .\local_config\runtime.real.yaml
Copy-Item .\.env.example .\.env
```

在 `.env` 中填写 `ONTOLOGY_LLM_MODEL`、`ONTOLOGY_LLM_BASE_URL`、`ONTOLOGY_LLM_API_KEY`；本地模型服务需兼容 Chat Completions 的结构化工具调用，并在运行前启动。`stream` 和 `thinking` 参数要按服务实际支持情况设置。示例真实配置默认关闭企业 MCP 和外部本体；启用 MCP 时，其服务也须在离线环境可达。[LLM 配置说明](LLM_CONFIGURATION.md)

可选的本地向量召回还需把完整的 Qwen3-Embedding-0.6B 模型目录复制到 Windows，并将 `.env` 的 `ONTOLOGY_EMBEDDING_MODEL_PATH` 改为该机器上的路径，再开启 `embedding.enabled`。现有 Windows 离线 bundle 是路线2基础依赖闭包，**尚未包含 embedding 所需的模型文件及其单独依赖**；需要为 Windows x64 / Python 3.14.7 准备并实机验证兼容轮子。没有这些材料时保持默认关闭，基础模拟运行仍不需要向量模型。路线2不对百万行记录逐行编码；本地向量检索的范围和上限见 [LLM 与向量配置](LLM_CONFIGURATION.md)。

```powershell
.\.venv\Scripts\python.exe -m ontology_r2.cli build --config .\local_config\runtime.real.yaml --output .\runs\win-real-001
```

查看 `manifest.yaml` 的导入行数、运行上限、未处理范围和错误。`max_object_records`、`max_relation_records` 是明确的预算；达到上限会产生 `partial`，不能据此宣称全量关联已发现。[现有功能边界](STATUS.md)

## 4. 可选：导出并导入 DataHub Lite

从已有构建结果导出 DataHub 文件，无需安装 Lite 或 SDK：

```powershell
.\.venv\Scripts\python.exe -m ontology_r2.cli export-datahub --run .\runs\win-mock-001 --output .\runs\win-mock-001\datahub-metadata.json --platform postgres --environment PROD
```

`postgres` 是当前给 GaussDB 兼容目录选用的默认 DataHub 平台键；接收方使用其他平台键时应显式修改。文件含表和列的说明、原始类型、声明主键及来源路径/哈希，仍属于内部元数据。它不含路线2后续抽取的实例关系。

若需试验 Lite，须已在第2节**首次安装**时加上 `-WithDataHubLite`。然后运行本地导入脚本；它仅接受 `datasetProperties`、`schemaMetadata`，写入 DuckDB 后按 URN 和 aspect 逐项读回核对：

```powershell
.\.venv-datahub\Scripts\python.exe .\scripts\import_datahub_lite.py .\runs\win-mock-001\datahub-metadata.json --catalog .\runs\win-mock-001\datahub-lite.duckdb
```

该脚本在导入 DataHub SDK 前显式设置 `DATAHUB_TELEMETRY_ENABLED=false`。开发阶段用本机安装的 `datahub check metadata-file` 命令检查过导出格式：CLI 是本地程序，并非上传接口；但上游 CLI 默认启用使用情况遥测。若自行运行 CLI，请先在 PowerShell 执行 `$env:DATAHUB_TELEMETRY_ENABLED='false'`。路线2构建和 Lite 导入脚本都不要求运行 CLI。

DataHub 官方也提供 [Lite 导入 metadata-file 的命令](https://github.com/datahub-project/datahub/blob/master/docs/datahub_lite.md#importing-from-a-file)。Lite 的本地导入只能验证文件互操作，不能验证图遍历或血缘。目标轮子的跨平台下载、依赖闭包检查和本地 Lite 读回已完成；**以上 Windows 安装、构建及导入命令尚未在 Windows 真机验证**。真机运行时应保留两个环境的 `pip check`、构建 `manifest.yaml` 和 Lite 导入结果。

本机验证 DataHub CLI 时还出现了“Python 3.11 以上版本尚未积极测试”的上游提示。目标电脑的 Python 3.14.7 因而需要实机确认 Lite 行为；若 Lite 不兼容，可只为 Lite 另装 Python 3.11，路线2本地构建不依赖 Lite。
