---
name: comsol-parameter-task
description: 在已连接的 COMSOL 模型上执行专家明确指定的单参数修改，使用结构化任务、只读预览、本机专家批准和回读核验。适用于参数修改任务；不用于网格、求解或开放式建模分析。
---

# COMSOL 参数任务

按 [参数任务协议](../../../docs/task-protocol.md)构造 `protocol_version="1.0"` 的任务。专家在 COMSOL Desktop 或内部完整 MCP 中取得精确 tag，并选择交给执行 Agent 的参数信息；不从历史报告推断当前模型。Agent 应只接管该 tag 并读取任务所需参数；受限工具仍可对其他已知 tag/参数名调用，实际可读范围取决于部署环境权限与专家规则。外部 GPT 不直接连接 MCP；哪些上下文可交给它，由专家按公司规定选择。

1. 明确参数名、预期当前表达式和新表达式；缺失时通过 `param_get` 只读核对已选参数，并交专家确认。构造仅含 `param_set` 的任务，其 `expected_result.parameter_expression` 与目标新表达式一致。
2. 在 `observe` 下调用 `task_parameter_preview(model_name=<精确 tag>, task=<任务 JSON>)`。把完整任务、实际读到的表达式及任务指纹交专家审核。此步不修改模型，任务状态为 `pending`。
3. 等待专家在本机交互终端运行 `comsol-task-approve <task_id> --data-dir <MCP 数据目录>`，阅读预览并输入指定确认短语；Agent 不得代为运行批准 CLI。终端不自动继承 MCP 子进程的数据目录配置。专家告知已批准后，调用 `task_parameter_status(task_id)` 核对状态为 `approved`。MCP 不提供批准工具。
4. 对同一 tag 显式调用 `model_access_set(..., access_mode="write")`，再调用 `task_parameter_execute(model_name=<精确 tag>, task=<未改动的任务>)`。结束时把外部接管模型切回 `observe`，执行失败也尝试复位。
5. 检查结构化回读事实，并由专家验收。遇到任何前置值变化、工具失败或回读不一致，停止重试与后续写入；可用 `task_parameter_status(task_id)` 查询本地记录，报告实际状态供人工审核。不要假定失败写入已回滚。MCP 重启、重连或重新接管模型后，旧预览不得执行，须用新 `task_id` 重新预览与批准。

本流程不自动建网格、求解或保存模型。任务状态可供查询，但计算排队与自动接续尚不在此 Skill 范围内。
