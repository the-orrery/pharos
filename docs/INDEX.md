---
description: "pharos 的长期文档域：项目架构、行为契约、运行知识和接手资料。源码、测试、配置和运行态数据不直接作为 KB note。"
keywords: [pharos, docs, architecture, KB纳管, CLI]
kind: index
---

# pharos docs

这里放 `pharos` 的长期知识文档。源码、测试、配置、lockfile 和运行态数据是工件，不直接作为 KB note；需要被长期召回的知识应写成本目录下的 reference、spec、decision 或 runbook。

当前入口：

- [[overview]]：解释 pharos 的定位、check / ticket / notification 三个概念和运行主流程。
- [[architecture]]：仓库开发地图；说明项目是什么、模块怎么分、关键不变量、主路径和“改 X 去哪”。

维护规则：

- 新增稳定约束时，补 `*-contract.md` 或 `*-spec.md`，`kind: spec`。
- 新增架构取舍时，补 ADR/decision；不要把 why 写进 `architecture.md`。
- 新增操作流程时，补 runbook/how-to；不要把步骤堆进 `architecture.md`。
- 文档涉及可漂移事实时，应写明代码入口或重验命令。
