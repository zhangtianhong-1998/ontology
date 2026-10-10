# 固定来源样本核对

`route2/scripts/audit_semantic_samples.py` 只读核对 12 个固定来源样本，不是语义准确率基准，也不能替代完整业务验收。C1–C3 只适用于 `semantic_contract_control` 输入，F1–F9 只适用于 `fruit_data_v2` 全量输入。其他输入中的缺席样本标为不适用，不计作抽取失败。

在 `route2` 目录运行，例如：

```bash
mkdir -p local_config
.venv/bin/python scripts/audit_semantic_samples.py \
  runs/semantic-contract-control-live-v3-20260928 \
  --output local_config/control_v3_sample_audit.json
```

替换运行目录即可核对全量产物；省略 `--output` 时输出到终端。脚本不调用模型，以只读模式访问结果库，禁止把报告写入来源输入或运行产物目录。

输出区分 `found`、`not_found`、`unresolved`、`rejected`、`conflict`。`found` 只表示该项明确列出的约束得到支持，例如 C3 不证明事实绑定成功；部分样本仍需要人工判断计算依赖或关系含义。报告保留来源文件哈希、记录位置、证据 ID 和类型映射依据。

类型归属只采用已接受的 exact alignment 或经核验的定义模板成员。`source_refs` 中的关联、依赖上下文不证明身份相同；模板成员也不证明源实体相同。回归测试包含错误合并、错误分类、参数错放及三种映射边界：

```bash
.venv/bin/python -m pytest tests/test_semantic_sample_audit.py -q
```
