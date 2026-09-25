---
name: comsol-model-edit
description: 将明确的 COMSOL 操作计划组织成通用 API、计算和文件工具调用，读取验证结果并衔接后续步骤。
---

# COMSOL 计划执行

## 定位对象

通过模型发现和节点读取，将任务中的查找条件对应到实际模型 tag、节点路径和 API 访问链。定位完成后使用这些真实标识执行本轮步骤。

## 修改设置

参数、材料、物理场、边界选择、几何特征和研究设置统一使用 `comsol_api_write`。每次调用传入一个操作的 `steps`、`method`、类型明确的 `args`，以及：

- `preconditions`：操作前必须满足的读取条件。
- `before`：需要保留的修改前取证。读取失败会记录在报告中，操作仍继续。
- `verify`：操作后的读取与比较条件，至少一项。

工具完成这些内部步骤。读取 `write_attempted`、`write_returned`、`after` 和 `verification`，再按计划衔接下一步。创建节点可在 `verify` 中读取新节点，或检查父集合的 `tags()` 是否包含新 tag；删除节点则检查父集合是否已无该 tag。

## 构建、计算和文件操作

- 几何构建用 `geometry_build`；导入已有 Import 节点用 `geometry_import`。节点类型和属性由任务通过通用 API 配置。
- 网格构建用 `mesh_build`，网格设置通过通用 API 配置。
- `study_solve` 返回后台任务的 `run_id`。用 `study_get_progress` 或 `study_wait` 跟踪同一任务，取得最终状态后处理依赖求解的步骤。进度百分比以工具实际返回为准。
- 当前 `study_cancel` 返回取消能力不支持的事实；保留原运行标识并继续查询状态。
- `results_evaluate` 计算任务指定的已有 numerical 节点；表达式、单位、数据集、解的选择先按计划用通用 API 设置。`results_solution_info` 读取指定数据集的解信息。
- `results_export` 执行指定导出节点，保留实际文件属性、路径和执行结果。
- `model_save` 按指定路径保存；`save_copy=True` 保存副本并保留模型原保存位置。`model_clone` 在指定路径保存当前模型快照，再以新 tag 加载副本。

具体参数以 MCP 工具定义为准，交接例子见 [任务与报告约定](../../../docs/task-protocol.md)。每次调用后按其中的“报告续页”取齐事实，再组织后续步骤；整轮报告与异常处理遵循 [AGENTS.md](../../../AGENTS.md)。
