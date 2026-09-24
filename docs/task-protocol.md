# 参数任务协议 1.0

本文定义方案 A 的首个可执行 PoC：修改已确认模型的一个参数。它连接“专家与 AI 整理技术意图”和“本地 MCP 执行并回读”。目标架构见 [方案汇报材料](comsol-proposal.md)。

## 边界与角色

- 专家决定问题、可外发的模型上下文、目标表达式、执行授权和最终验收。外部 GPT 不直接连接 MCP；哪些真实模型信息可交给它辅助分析与编译任务，由专家按公司规定选择。
- 专家在 COMSOL Desktop 或内部完整 MCP 中取得当前模型的**精确 tag**，只把该 tag 和已选参数信息交给执行 Agent。不能用显示名称猜测，也不能复用过往临时验收的 tag。
- 执行 Agent 使用 `src.task_server` 受限入口。工具面收窄为连接已有 Server、按 tag 接管/解除接管模型、切换访问模式、`param_get` 和预览/执行/查询参数任务；通用 `param_set`、模型全量发现/检查、参数列表、网格和求解工具均不暴露。`model_attach` 和 `param_get` 仍可对已知 tag/参数名调用；实际可读范围依赖部署环境权限与专家规则。完整 `src.server` 供专家在内部手动操作。
- 本地执行端应只读取任务所需的真实参数表达式。参数任务仅执行指定的 `param_set`，不会自动生成网格、求解、保存或调度计算任务。

## 输入任务

`task_parameter_preview` 和 `task_parameter_execute` 接收同一个 `task` JSON，以及单独的 `model_name`（值必须等于 `task.model_tag`）。批准不通过 MCP 工具或任务字段传递。

```json
{
  "protocol_version": "1.0",
  "task_id": "expert-task-001",
  "goal": "把已确认的长度参数改为 12[mm]",
  "model_tag": "model1",
  "target": {
    "parameter": "L",
    "expected_current_expression": "10[mm]",
    "new_expression": "12[mm]"
  },
  "allowed_operations": ["param_set"],
  "expected_result": {
    "parameter_expression": "12[mm]"
  }
}
```

上例仅展示格式；`model1`、`L` 和表达式须替换为当前 Server 的真实值。各字段含义如下。

| 字段 | 要求 |
| --- | --- |
| `protocol_version` | 当前固定为 `"1.0"`。 |
| `task_id` | 本次任务的唯一标识，用于查状态与防止已执行任务重放；修改目标须新建任务。 |
| `goal` | 专家已明确的自然语言目标，仅用于说明意图，不扩大允许的操作。 |
| `model_tag`、`model_name` | 两者须为同一个精确 COMSOL tag。 |
| `target.parameter` | 已存在的参数名。 |
| `target.expected_current_expression` | 专家预期的原始表达式字符串，和实时读取值比较；它不是数值求值结果。 |
| `target.new_expression` | 准备写入的完整 COMSOL 表达式，含所需单位。 |
| `allowed_operations` | 当前只接受 `["param_set"]`。 |
| `expected_result.parameter_expression` | 必须等于 `new_expression`，用来检查写入后的实际表达式。 |

## 预览、确认与执行

1. **只读预览。** `task_parameter_preview(model_name, task)` 校验协议、定位精确模型并读取参数当前表达式。若与 `expected_current_expression` 不一致，应返回冲突。成功预览返回任务事实与指纹，在本地保存 `pending` 记录；模型不被修改。
2. **专家独立批准。** 向专家展示完整任务和预览中的真实模型 tag、参数现值、目标表达式及指纹。专家在本机交互终端运行 `comsol-task-approve <task_id> --data-dir <MCP 数据目录>`，阅读 CLI 显示的预览并输入指定确认短语，任务才变为 `approved`。批准 CLI 必须显式指向 MCP 配置的 `COMSOL_MCP_DATA_DIR`；该环境变量不会自动从 MCP 子进程传到终端。Agent 不得代为运行该 CLI；MCP 不暴露批准工具。员工助手远程批准接入尚未实现。
3. **核对批准并开启写入。** 专家告知已批准后，Agent 通过 `task_parameter_status(task_id)` 确认 `approved`，再对同一 tag 调用 `model_access_set(..., access_mode="write")`。执行工具本身不自动提高访问权限。
4. **受控执行。** 调用 `task_parameter_execute(model_name, task)`。执行工具核对批准记录、任务指纹、模型和写入前的实际表达式，仅修改指定参数，然后重新读取原始表达式。结构化返回区分修改前事实、实际执行动作、回读结果和错误/不一致。
5. **恢复与验收。** 操作后把外部接管模型切回 `observe`；如执行失败也尝试复位。专家检查模型实际变化与返回事实，决定是否验收。

工具失败、前置值冲突或回读不一致时，停止自动重试和后续写入。尤其写入后发生错误时，不能推断模型已回滚；查询 `task_parameter_status(task_id)` 并人工核对真实模型。已执行的 `task_id` 不得重放。

## 状态范围

本 PoC 在本地 SQLite 中记录参数任务的 `pending`、`approved`、运行中和终态结果，`task_parameter_status(task_id)` 可在 MCP 重启后查询已有记录。状态查询是审计与恢复线索，不代替 COMSOL 真实状态回读。旧预览在 MCP 重启、重连或重新接管模型后不能执行；须用新 `task_id` 重新预览并批准。此处尚无计算任务队列、持续运行的调度器、任务自动接续或员工助手远程批准入口；这些按提案后续阶段建设。

本机交互 CLI 与 SQLite 不是独立身份认证。当前本机 Codex 配置通常让 MCP 与 Agent 使用同一系统账号；若 Agent 能使用任意 shell 或直接写任务数据库，就可能绕过审批。公司正式部署需要将 MCP、审批和数据目录置于可信服务边界，限制 Agent 的任意 shell/数据库访问，并接入员工助手的可信身份与审批接口。当前 CLI 只用于 PoC 人工验收。

真实验收应在具备可用 License 的公司电脑上，使用临时测试模型与明确的专家确认完成；本地无空余 License 时不运行 COMSOL 验收。

## 公司电脑人工验收

以下步骤在有空余 License 的公司电脑上由专家操作，不复用历史验收的临时 tag。

1. 克隆仓库并安装环境：macOS/Linux 运行 `python3 scripts/bootstrap.py`；Windows 运行 `py scripts/bootstrap.py`。在 Codex 中配置受限的 `examples/codex.task.config.toml.example`，不要为参数任务启用完整工具模板。
2. 在已有多客户端 COMSOL Server 上建立临时模型和一个已知表达式的测试参数，并让 COMSOL Desktop 连接同一 Server。专家在 Desktop 或内部完整 MCP 中取得**本次**精确 tag，只把 tag 与所选参数信息交给执行 Agent；Agent 通过受限 MCP 的 `comsol_connect`、`model_attach` 接管该 tag。
3. 构造仅修改该参数的任务，调用 `task_parameter_preview`；核对参数现值、目标表达式、任务指纹及 `pending` 状态。
4. 专家在本机交互终端运行批准 CLI，显式传入与受限 MCP 配置相同的数据目录，阅读预览并输入 CLI 要求的确认短语。Agent 不运行这些命令。

   macOS/Linux：

   ```bash
   ./.venv/bin/comsol-task-approve <task_id> --data-dir /ABSOLUTE/PATH/TO/local-comsol-mcp-data
   ```

   Windows PowerShell：

   ```powershell
   .\.venv\Scripts\comsol-task-approve.exe <task_id> --data-dir C:\ABSOLUTE\PATH\TO\local-comsol-mcp-data
   ```
5. 专家告知已批准后，Agent 查 `task_parameter_status(task_id)` 确认为 `approved`，再对同一 tag 切换 `write`、调用 `task_parameter_execute(model_name, task)`，最后恢复 `observe`。
6. 对照返回的 `read_state_after.parameter_expression` 与 COMSOL Desktop 中同一模型参数的实际表达式；任何不一致均停止后续写入并由专家审核。验收无需建网格、求解或保存模型。
