# V14 小规模工程验收

本目录保存 2026-10-07 的小报告，不包含旧 V13 全量产物或企业数据。全部语义提案为本地固定决策，外部 LLM 调用 0 次；程序编译、来源验证和事实重放使用生产实现。

- [事实闭环报告](controlled_report.json)：4 类型、2 配置关系、2 事实；追加数据后复用生成 3 事实。范围或定义变更会拒绝绑定；候选限额 1 时明确遗漏 2。
- [关联调度回放](association_schedule_report.json)：只读 V13 候选/核验文件；12 个名额为 6 范围歧义、6 部分匹配，不代表接受 12 条关系。
- [验证摘要](validation.json)：全套 1385 项通过；无新全量抽取、无新版浏览器交互验收。

在仓库 `route2/` 目录复现小实验：

```bash
uv run python scripts/verify_correctness_v14.py --output runs/correctness-v14-check
```

源 CSV/YAML、编译计划、关系、事实、来源证据与复用包都会写入新目录。详细需求、边界和后续任务见 [V14 整改](../../docs/route2/CORRECTNESS_V14.md)。新增快照仍需重新扫描和验证；没有完成通用关系重放、持久实体身份、公式求值或未知数据的模型质量评估。
