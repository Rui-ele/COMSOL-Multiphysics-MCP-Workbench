# COMSOL MCP Workbench 项目规则

本仓库的目标架构见 [docs/comsol-proposal.md](docs/comsol-proposal.md)。下列规则适用于通过本项目 MCP 操作真实 COMSOL 模型；日常源码开发仍按用户当前任务进行。

## 数据与专家职责

- 模型、参数、结果及项目背景遵守公司的保密边界。外部 GPT 不直接连接 MCP；向其提供哪些上下文，由专家按公司规定选择。执行 Agent 不自行批量读取或外发模型内容。
- 专家决定技术方案、目标模型、写入授权和结果验收。Agent 可以读取、整理任务与执行已确认操作，但不能把分析建议当成修改许可。
- 专家在 COMSOL Desktop 或内部完整 MCP 中取得当前模型的精确 COMSOL tag，再把该 tag 和已选参数信息交给执行 Agent。Agent 应只接管该 tag 并读取任务所需参数；受限工具对其他已知 tag/参数名仍可调用，实际可读范围取决于部署环境权限与专家规则。历史验收中的 tag 不可复用；模型或参数不明确时请专家核对。

## 结构化参数任务

- 单参数修改使用 [参数任务协议](docs/task-protocol.md)与 `.agents/skills/comsol-parameter-task/SKILL.md`。普通 `param_set` 是底层工具，不能绕过参数任务的预览、确认和核验门禁。
- 先调用只读的 `task_parameter_preview`，向专家展示精确任务、模型现值、目标值及任务指纹。预览在本地留下 `pending` 记录，不修改模型。
- 批准只能由专家在本机交互终端运行 `comsol-task-approve <task_id> --data-dir <MCP 数据目录>`，阅读预览并输入指定确认短语。终端不会自动继承 MCP 子进程的 `COMSOL_MCP_DATA_DIR`；MCP 不暴露批准工具，Agent 不得自行运行批准 CLI。员工助手远程批准接入尚未实现。
- 专家告知已批准后，Agent 查询 `task_parameter_status(task_id)` 确认状态为 `approved`，再对已确认的精确 tag 调用 `model_access_set(..., access_mode="write")` 和 `task_parameter_execute(model_name, task)`。执行工具会重新核对任务与前置值、修改并回读。操作后将外部接管模型切回 `observe`，即使执行失败也应尝试复位。
- 若模型、任务、当前值、预览或回读不一致，停止自动继续与重试，返回实际事实供专家审核。写入后报错不等于已回滚；先查询 `task_parameter_status` 并核对真实状态。
- 任务状态可跨 MCP 重启查询，但旧预览在 MCP 重启、重连或重新接管模型后不能执行；需要新 `task_id` 重新预览和批准。
- 参数任务不隐含建网格、求解、保存、导出或提交下一任务。此类操作须另有明确任务和授权。

本 PoC 的本地状态记录只覆盖参数任务的预览与执行，不代表计算任务排队、自动接续或员工助手集成已实现。
