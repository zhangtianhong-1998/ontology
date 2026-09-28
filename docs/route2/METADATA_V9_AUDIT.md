# V9 元数据与前端核验

日期：2026-09-29。核验对象为 `route2/runs/fruit-templates-full-v9-20260928`，输入为本地合成数据 `route2/local_data/fruit_data_v3`。本次未调用 LLM，未重写 V9 原产物。

## 结论

V9 元数据内容与输入的 23 张表一致。没有发现漏列、错列、声明约束被改写、DataHub 字段错位或悬空边。原页面存在独立的交互和排版错误：默认隐藏节点名；SVG 在鼠标按下时立即捕获指针，导致普通点击无法落到目标节点；实例列表与详情采用不同的类型字段和重复记录优先级；元数据的力导布局将关联多的节点压在中心。这些问题已修复。

元数据正确不代表业务本体正确。2046 条技术关联来自程序核验，只证明字段匹配结果被忠实保留，未证明其业务语义成立，也不是 2046 条外键或血缘。

## 核验方法与结果

| 对照项 | 方法 | 结果 |
|---|---|---|
| 23 张表 | 逐项比较 schema、名称、注释和表属性 | 一致 |
| 445 列 | 比较列名、顺序、原生类型、非空、注释、默认值及 table_has_column 端点 | 一致 |
| 20 条约束 | 比较每个原始约束字段和所属表 | 一致 |
| 声明外键 | 对照 23 个 foreign_keys YAML | 输入和图均为 0 |
| 92 个来源文件 | 对 69 个 YAML 和 23 个 CSV 重新计算 SHA-256 | 全部一致 |
| 23 个源行类型 | 每张表对应一个 source_record_type，检查映射边和类型类别 | 一致 |
| 2046 条技术关联 | 对照 association_rules 的端点、状态、条件、作用域、转换、核验结果 | 一致 |
| 46 个 DataHub aspects | 对照每表 datasetProperties / schemaMetadata 的描述、字段、原生类型及空值约束 | 一致 |
| 图身份与端点 | 检查重复节点 ID 和所有边两端 | 无重复 ID、无悬空边 |

DataHub 在此是本地元数据契约与导出适配层：`metadata_graph.py` 保存原始表/列/来源，`datahub_adapter.py` 生成 URN 和 MCP JSON。本次没有连接或上传 DataHub 服务。

另外修复了适配器中一个通用类型问题：`integer[]`、`boolean[]` 等数组之前可能先命中标量规则，现优先识别数组。V9 的原生字段对照不受此问题影响。

### 逐表清单

全部位于 `fruit_market` schema；声明约束为主键。每行均完成列内容、列归属、约束和 DataHub 字段对照。

| 表 | 列 | 约束 | 声明外键 | 对照结果 |
|---|---:|---:|---:|---|
| fruit_api_input_param | 15 | 1 | 0 | 通过 |
| fruit_api_output_param | 14 | 1 | 0 | 通过 |
| fruit_api_tag | 13 | 1 | 0 | 通过 |
| fruit_business_rule | 12 | 1 | 0 | 通过 |
| fruit_dashboard_card | 34 | 1 | 0 | 通过 |
| fruit_db_connection | 19 | 1 | 0 | 通过 |
| fruit_dim_anchor | 15 | 1 | 0 | 通过 |
| fruit_dim_combo_rule | 14 | 1 | 0 | 通过 |
| fruit_dim_definition | 24 | 1 | 0 | 通过 |
| fruit_dim_field | 19 | 1 | 0 | 通过 |
| fruit_dim_member | 20 | 1 | 0 | 通过 |
| fruit_import_staging_temp | 70 | 0 | 0 | 通过 |
| fruit_market_api | 24 | 1 | 0 | 通过 |
| fruit_measure_def | 24 | 1 | 0 | 通过 |
| fruit_metric_anchor | 14 | 1 | 0 | 通过 |
| fruit_metric_attr | 16 | 1 | 0 | 通过 |
| fruit_metric_detail | 16 | 1 | 0 | 通过 |
| fruit_op_log | 17 | 1 | 0 | 通过 |
| fruit_org_dim_data | 15 | 0 | 0 | 通过 |
| fruit_param_ref_rule | 13 | 1 | 0 | 通过 |
| fruit_synonym_dict | 6 | 0 | 0 | 通过 |
| fruit_wide_table_column | 14 | 1 | 0 | 通过 |
| fruit_wide_table_def | 17 | 1 | 0 | 通过 |

### 图层边界

- `declared_fk`：仅来自输入外键声明；V9 为 0。
- `technical_link`：仅来自已核验字段关联；保留 `lineage_inferred: false` 与 `meaning: checked_field_association_only`。页面把纯数字重合风险单独收起，可展开查看；未改写原证据。
- `mapped_as_source_record_type`：物理表到源行结构的映射，不是业务概念等价声明。
- 本体主图只画已接受类型、继承和明确端点的对象关系。配置定义实例和实际经营观测分别标明，不混入类型图。

## 前端修复与实际验收

- 普通点击不再捕获指针；超过 4px 的拖动才进入画布/节点拖动。完整名称标签和关系名称都绑定自身 ID。
- 默认展示完整名称，字号保持实际 12px；继承边用实线箭头说明，悬停显示名称，避免重复“派生自”覆盖节点。
- 关系文字绕开节点和标签；元数据按关联分组排到网格，表名在画布、完整注释在详情。
- 选中后清空原详情滚动位置；属性优先，来源和模板证据折叠在下方。
- 实例按 `ontology_type_id` 或 `type` 统一索引、去重；按类型和实例类别各取有限样本，显示总量与预览范围，避免某张大表占满全部名额。配置定义实例显示原始字段值、槽位取值、来源和证据，明确不等于经营观测。
- `render_viewer(..., output_path=...)` 可写新的独立预览，不覆盖历史运行。

使用本机缓存的 Chromium headless，通过只服务新 HTML 的临时 `127.0.0.1` HTTP 服务实测；未访问曾被拒绝的旧 file URL。已检查实际截图，不只是执行 DOM 辅助函数。

| 浏览器验收 | 结果 |
|---|---|
| 1440×1000 初始视野 | 38 个节点名称全部可见，实际字号约 12px，节点标签无相互重叠 |
| 38 个节点名称逐个实际点击 | 详情 ID 和名称全部对应正确 |
| 3 条业务关系名称实际点击 | 详情关系 ID 全部正确 |
| 切换详情 | 滚动位置复位 |
| 缩放、拖动固定、重新排布、搜索 | 实际鼠标操作通过 |
| 元数据 23 表、展开列并点击 | 正确显示列名、原生类型和所属表 |
| 600×900 初始视野 | 38 个名称全部可见，约 12px，节点标签无相互重叠；点击和缩放/适配通过 |
| JavaScript 异常 | 0 |

窄屏采用较高画布和上下排列详情；不会为了全部挤入一屏而缩小文字。高密度边仍需聚焦邻域阅读，这不是对语义关系质量的验收。

代码回归：`test_viewer_details.py`、`test_viewer_geometry.py`、`test_datahub_adapter.py` 合计 **39 passed**。另有真实 258 节点拓扑的布局、路由、镜头和拖动回归。新增实例预览以合成测试验证，不把尚未重跑的新实例物化结果当作 V9 已有产物。

## 本地证据

原始核验 JSON、可复现核验脚本、浏览器操作脚本、截图和新预览保存在：

`route2/local_config/v9-viewer-review-20260929/`

文件：`metadata_audit.json`、`audit_metadata.py`、`browser-check.json`、`verify_browser.cjs`、`initial-wide.png`、`initial-narrow.png`、`selected-wide.png`、`metadata-wide.png`、`viewer.html`。此目录属于本地实验文件；本报告可随代码保存。
