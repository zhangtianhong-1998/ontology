# 路线1：开发顺序与完成标准

本文件是实施清单，不是已完成记录。每个任务先完成可运行的小范围端到端结果，再扩大输入。

| 任务 | 关联 US | 要写的最少代码 | 完成标准 |
|---|---|---|---|
| R1-T01 | R1-US01、R1-US02 | 共享数据模型、安全 YAML 读取、根模型验证 | C01/C02；五根和关系 kind 正确；原始注释与复合键无损 |
| R1-T02 | R1-US03、R1-US07 | 一个 CLI 和 1A 按表流水线 | 合成三表跑完；无 CSV、MCP、外部模型和旧 core 访问 |
| R1-T03 | R1-US05、R1-US06 | Delta 校验、Judge、有限修复、缓存和导出 | 错误 ID/越界根被拒；响应回放相同；失败不冒充成功 |
| R1-T04 | R1-US04 | RDF ExternalCard、一个模型的对齐召回 | 1B 与 1A 只差外部模块；误匹配案例能 unmapped |
| R1-T05 | R1-US04 | CDM JSON 适配和导入损失报告 | JSON 不走 RDF 解析；复杂语义缺失可定位 |
| R1-T06 | R1-US06、R1-US07 | 最小评测命令及清单 | E1A/E1B 共用输入/答案；成本完整；文档命令可执行 |

计划命令，待实现后才可运行：

```bash
python -m r1 build --config 子route1/config/no_external.yaml --run-id e1a-001
python -m r1 build --config 子route2/config/with_external.yaml --run-id e1b-001
python -m r1 evaluate --run runs/e1a-001 --gold fixtures/schema_gold.yaml
```

第一批测试只覆盖有实际风险的行为：输入丢失、空 FK、复合键、关闭来源、非法根/引用、预算、冲突和缓存。不要给每个无逻辑包装函数单独写镜像测试。

简洁代码要求：流水线控制只放在一个入口；配置不使用动态插件注册框架；不要为 1A/1B 分叉复制生成器；同一根模型只维护一个文件；错误应有具体来源和原因。
