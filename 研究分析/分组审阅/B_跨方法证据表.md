# 文献 5—10：可直接用于总报告的证据表

均为作者报告结果，未重跑。数值差以绝对 F1 或百分点表示；不同任务、输入先验和模型预算的行不能横向排综合优劣。

| 方法/待检验主张 | 输入先验 | 可用量化证据 | 可成立的结论 | 不可外推的结论 | 精确出处 |
|---|---|---|---|---|---|
| SBU：embedding 是否优于词面方法 | OBI 已给类型/父候选 | token-overlap F1=0.34；4 种 embedding 均约 0.35 | 此 OBI 设置下 embedding 增益约 0.01 | embedding 普遍无用；相似度可等同 is-a | [5.pdf](../../文献/5.pdf) p13 Table 4；[DOI](https://doi.org/10.52825/ocp.v6i.2887) |
| SBU：无微调是否可完成类型判定 | 术语、类型清单、训练示例 | Claude Batch，B/OBI 0.94、MatOnto 0.57、SWEET 0.69 | 给定类型的分类可以有较好成绩 | 无标签/无模式构建完整本体；批处理独立造成全部增益 | 5.pdf p13 Table 3 |
| SBU：批量提示是否稳定更好 | 相同任务但常换模型 | A1/Engineering DeepSeek 单次 0.58、Batch 0.57 | 批量未必有益，需同模型消融 | Batch 普遍胜过逐文档 | 5.pdf p12 Table 2 |
| Phoenix：CoT 已验证有效吗 | 文档+步骤/示例提示 | A1 学术最佳 F1=0.3950；工程最佳 0.2556；A2 工程最佳 0.1846 | 该组合可抽取且有明显领域差异 | CoT 独立增益已证实；本文没有无 CoT 对照 | [6.pdf](../../文献/6.pdf) p5 Table 1；[DOI](https://doi.org/10.52825/ocp.v6i.2888) |
| LABKAG：保留原文上下文 | 已抽取实体，给领域示例 | A1 学术 F1 23.81%→70.00%；A2 学术 51.85%→83.08% | 完整文档/联合分类组合优于仅生成描述 | 全部增益只来自原文；两版本也改变了批处理粒度 | [7.pdf](../../文献/7.pdf) p7 Table 2；p4 §3.1.2；[DOI](https://doi.org/10.52825/ocp.v6i.2891) |
| LABKAG：规则扩展是否有益 | 工程单位前缀规律 | F1 59.63%→66.61%，R 43.88%→73.86%，P 93.02%→60.66% | 针对 Gold 生成规律扩展能以精确率换召回 | 纯文本证据抽取提高；真实业务可无验证枚举所有变体 | 7.pdf p7 Table 2；p4–5 §3.1.3 |
| LABKAG：领域泛化 | 已给类型与高层分类提示 | C/MatOnto 48.36%、SchemaOrg 65.01%，FoodOn 2.15%、PO 3.57%、Blind 4.85% | 强领域差异，示例依赖明显 | 提示方案已实现领域无关高质量构建 | 7.pdf p9 Table 3、p10 §5 |
| DREAM：模型审议收益 | 固定类型列表+5-shot | OBI 最佳单模型 0.851→审议 0.908；SWEET 0.548→0.593；MatOnto 0.568→0.568 | 选定 judge 在 2/3 域有收益 | 多模型稳定提升；没有多数投票/等成本对照 | [8.pdf](../../文献/8.pdf) p6 Table 2、p7 Table 3；[DOI](https://doi.org/10.52825/ocp.v6i.2892) |
| IRIS：增强与定义缓解稀疏 | 给定术语/类型，多标签微调 | B/MatOnto V 0.097、A 0.500、D 0.312、A+D 0.667；B/OBI A 0.839>A+D 0.828 | 联合有益但非处处最好 | 添加语义描述普遍提高所有域/任务 | [9.pdf](../../文献/9.pdf) p12 Table 3；[DOI](https://doi.org/10.52825/ocp.v6i.2895) |
| IRIS：过滤与定义改进层级边 | 类型清单；训练负例+推理候选共同变更 | C/OBI R 0.006、F 0.333、D 0.006、F+D 0.397；C/MatOnto 0.020、0.447、0.025、0.396；C/SWEET 0.002、0.182、0、0.252 | 联合候选/负例策略效果明显；定义要看域 | 过滤单独因果效应已隔离；candidate recall 上升已证实 | 9.pdf p13 Table 4；p11 §3.2.2 |
| IRIS：非层级关系 | SWEET 类型与关系表均给定 | D/SWEET R 0.058、F 0.391、D 0、F+D 0.532；F→F+D P 0.260→0.426，R 0.788→0.709 | 过滤基础上加定义提升 F1/精确率但降低召回 | 已自动发现未知关系种类；跨域效果已验证 | 9.pdf p13 Table 5 |
| CUET：混合方法优于相似度 | OBI 类型列表 | 混合 F1=0.1142、R=0.0744；embedding baseline F1=0.0771 | 该组合高于其基线，但绝对覆盖很低 | 充分高质量/可直接发布的 taxonomy | [10.pdf](../../文献/10.pdf) p9 Table 4；[DOI](https://doi.org/10.52825/ocp.v6i.2896) |
| CUET：级联保障精确率/成本 | SchemaOrg 训练类型作父候选、阈值、调用预算 | 最佳 F1=0.0866、P=0.0637、R=0.1350；作者报告少 78% 调用，未给调用日志；同任务纯 embedding 基线缺席 | 有级联实验原型；效果与成本需要再核验 | 高精确率已实现；端到端成本节省 78%；12.3% 同任务增益已可复核 | 10.pdf p8 §4.2.2、p9–10 §5、p10 Table 5 |

## 许可证与公开实现

| 系统 | 2026-09-20 核验 | 证据 |
|---|---|---|
| SBU | 实现公开，MIT | [仓库/许可](https://github.com/rarahnamoun/LLMs4OL-Challenge-ISWC-2025/blob/main/LICENSE.txt) |
| Phoenix | 只有 README 与 .gitignore；无实现、无许可 | [仓库](https://github.com/MahsaSanaei/Phoenixes-LLMs4OL2025)；递归树本地快照 |
| LABKAG | 实现公开，未发现许可证 | [仓库](https://github.com/laboro-public/LABKAG-LLMs4OL-2025)；递归树/API license 本地快照 |
| DREAM | 实现公开，MIT | [仓库/许可](https://github.com/wpatipon-jaist/LLMs4OL2025-Task-B-DREAM-LLMs/blob/main/LICENSE) |
| IRIS | 实现/部分派生产物公开，Apache-2.0 | [仓库/许可](https://github.com/AFigaro/LLMs4OL_2025/blob/main/LICENSE) |
| CUET C1/C5 | notebooks 公开，均未发现许可证 | [C1](https://github.com/Mehreen1103/LLMs4OL-2025)、[C5](https://github.com/RehenumaIlman/LLMs4OL-2025-Large-Language-Models-for-Ontology-Learning) |

开源核验只证明当前可见代码与许可证状态，不代表模型权重许可、第三方数据许可或实验可复现性已经全面核查。

## 不应误用的原文疑点

- SBU p12 Table 2：同名 A2.3/Grok Batch 出现 0.62/0.30；不得选较高值当确定最佳。
- Phoenix p5 §4.2.1：混写不同数据集/模型的召回 0.5312/0.0840；应取表 1 对应行。
- LABKAG p10 Table 5：P=39.20%、R=45.31%、F1=45.32%，在同一聚合口径下不一致；不只是“平均 F1 未等于平均 P/R 调和均值”的问题，其 F1 超过两者上界。可以说明所列 P/R 的调和均值约 42.03%，但不能擅自更正论文 F1 为该值。
- IRIS：摘要“单独技术均提升”只能结合具体任务解释；C/SWEET 和 D/SWEET 仅定义均为 0，反例必须保留。
- CUET：12.3% 提升缺 C5 纯 embedding 基线，78% 节省缺调用明细；均保留“作者声称/未充分复核”标签。
