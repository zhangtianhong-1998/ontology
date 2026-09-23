# 字段关联统计的设计验证

这里验证统计分母、作用域、多义和原值转换的边界，不接入正式抽取管线。

- [cases.sql](cases.sql)：中性合成案例；没有真实企业数据。
- [verify.py](verify.py)：执行 SQL，检查 16 个预先写明的数值。
- 历史统计结果未打包；执行下方命令可生成自己的记录。

在路线2目录复查，用新的结果文件名：

```bash
uv run python design/field_association/verify.py --output design/field_association/results-local.yaml
```

已有结果不会覆盖。该实验不证明自动发现能找到所有候选，不证明 LLM 能识别引用含义，也不代表新统计模块已经实现。设计与方案比较见[字段统计设计](../../字段统计与关联发现设计.md)和[方案评估](../../字段关联方案评估.md)。
