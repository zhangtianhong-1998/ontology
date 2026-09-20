# 补充审查：数据库来源的本体与数据库到本体的映射

核验日期：2026-09-20。基于已下载原文逐页文本及原始网页/仓库，不把方法愿景当已验证结果。

## E12. Ontology Learning and Knowledge Graph Construction: A Comparison of Approaches and Their Impact on RAG Performance

Tiago da Cruz 等，2025；[arXiv原始页面](https://arxiv.org/abs/2511.05991)，当前页面仅列2025-11-08的v1，没有期刊/会议录用说明。

1. **确实包含数据库schema到本体学习，但不是全从零归纳。** 其RDB分支修改RIGOR，输入DDL、PK/FK和一般schema说明，且明确复用DINGO外部本体；然后用文本实例化KG。文本分支按句生成TTL、做语法修复。生成图有/无chunk两种形式。（PDF p4-5 §3.2）
2. **下游证据只有一个真实申请文档、20个人工问题。** 该葡语资助申请被译为英语并最小匿名化。人工判分：GraphRAG、RDB本体KG+chunk、文本本体KG+chunk均18/20完整正确；向量RAG 12/20；无chunk的RDB KG 4/20、文本KG 3/20。（p7实验，p8图4与§5.2）这较直接支持“保留文本证据上下文在此案例中很重要”，不能证明“数据库本体普遍优于文本本体”或“GraphRAG普遍优于充分调优的向量RAG”。
3. **成本与维护结论主要是推断。** 作者声称schema稳定故只需做一次学习，但未提供长期schema变更实验或完整token/费用对照；把它用于动态企业schema需重做验证。（p8-9结论/未来工作）[代码及数据仓库](https://github.com/tiagocrz/KGs_for_Vertical_AI)已核验公开；[LICENSE](https://github.com/tiagocrz/KGs_for_Vertical_AI/blob/main/LICENSE)为MIT，含DDL、语料、问题Gold、notebook、代码与结果。未运行复现。

## E13. Surprising Effectiveness of Self-Demonstrations in Enhancing Schema-Ontology Mapping with LLMs

Siddhesh Thombre、Manasi Patwardhan、Sunita Sarawagi；[arXiv原始页面](https://arxiv.org/abs/2609.13776)标明v1于2026-09-12提交，没有journal-ref、会议录用或出版DOI声明。PDF页眉“May 2026”不是正式发表证明；截至核验日应称**预印本**。

1. **任务是向已有本体生成映射，不是发现本体。** 输入已有类/属性/层次的本体、数据库schema及列值；输出SQL视图及R2RML，使数据库数据填充已有本体。按类→子类→数据属性→对象属性约束候选，再根据固定领域无关pattern库自动合成few-shot。该方法有预设pattern和本体先验。（p4-8 §3-4）
2. **覆盖RODI的3个场景，不是全部18个。** Conference-no-FK、Mondial、NPD-Atomic分别39、50、439对SQL/SPARQL查询，共528对。评测是执行结果元组的逐查询F1再平均。Gemini-2.0-Flash下各域0.79/0.79/0.51；跨全部查询平均0.56。去自示例后0.38，未分解直接生成0.10；人工示例0.53。故自示例在同任务控制实验的增益为0.18，强于仅用排名讲效果。（p8表1、p9指标、p11表2）
3. **仍有错误，且跨模型范围较小。** GPT-4o只测前两个域，0.56/0.65、平均0.61，不能与三域0.56直接比较。NPD仍有错误类/属性映射，前置domain推断错误会传播；没有证明所有schema都可无审核映射。（p13表3、p13-14错误分析）论文提供[匿名代码库](https://anonymous.4open.science/r/Schema-Ontology-Mapper-4BC1/)；本次网页工具无法访问、直接请求403，故仅核验到“作者提供地址”，**代码内容、许可和可复现性未核验**。（p6脚注1）

### E13关键表的对照边界

| 方法 | Conference-no-FK | Mondial | NPD-Atomic | 论文Avg | 原文 |
|---|---:|---:|---:|---:|---|
| Milan | 0.46 | 未报告 | 0.30 | 0.31 | p11表2 |
| LLM4VKG | 0.51 | 0.18 | 0.19 | 0.21 | p11表2 |
| Table-to-KG适配+程序映射 | 0.33 | 0.10 | 0.17 | 0.18 | p11表2 |
| Magneto适配+程序映射 | 0.41 | 0.28 | 0.21 | 0.23 | p11表2 |
| 手工few-shot | 0.72 | 0.82 | 0.48 | 0.53 | p11表2 |
| 本文完整方法 | 0.79 | 0.79 | 0.51 | 0.56 | p11表2 |
| 去self-demonstrations | 0.54 | 0.46 | 0.36 | 0.38 | p11表2 |
| 去matching与mapping分解 | 0.46 | 0.08 | 0.07 | 0.10 | p11表2 |

**审阅补充**：论文摘要“约25个百分点提升”对应0.56相对Milan的0.31，但Milan的Mondial为缺失，不能当成覆盖完全相同查询集合的配对提升。作者对传统基线引用既有文献结果，对部分LLM基线另做模型统一/任务适配；应说明基线来源差异。手工few-shot在Mondial反而为0.82，高于自示例的0.79，因此自动示例“整体相当或略好”不等于每域都赢。（p9-13）

## 主报告可直接采用的三句话

- 从数据库schema生成本体，和把数据库映射到已有本体，是不同任务；E12包含前者，E13主要验证后者，二者都需要外部本体/结构先验。（E12 p4，E13 p4）
- E13支持“分解匹配与SQL映射，再自动构造符合任务模式的示例”改善复杂映射；它没有证明从没有本体的企业数据自动发现完整领域本体。（E13 p4-8、p11表2）
- E12的18/20正确是保留文本chunk后的单案例下游结果，提示系统应保留原始证据；它既不是本体正确率，也不是可推广的企业级收益率。（E12 p7-9）
