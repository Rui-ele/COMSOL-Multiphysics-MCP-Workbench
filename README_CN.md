# COMSOL MCP Workbench

这是一套可独立安装、便于复用的本地 COMSOL MCP。它允许 MCP 客户端和
COMSOL Desktop 连接同一个多客户端 COMSOL Server，并共同操作服务器内存中的
同一模型。

项目目的、硬约束、可选技术方案及目标架构与任务编排，见[AI 辅助 COMSOL 方案汇报材料](docs/comsol-proposal.md)。

## 主要能力

- 发现并按稳定 tag 接管 Desktop 或其他客户端已经持有的模型。
- 外部模型默认进入只读 `observe` 模式。
- 只有显式切换到 `write` 后才能修改参数、重建或求解。
- 即使进入 `write`，MCP 也不能直接保存或删除外部模型。
- 记录精简审计日志并生成中文仿真交接报告。
- 提供真实双客户端验收脚本，验证跨客户端读写与生命周期保护。

## 结构化参数任务 PoC

本项目现在以[参数任务协议 1.0](docs/task-protocol.md)作为方案 A 的首个最小闭环：专家确认一个参数修改任务，MCP 先只读预览，再在专家明确确认后执行并回读。项目级操作规则见 [AGENTS.md](AGENTS.md) 和 [参数任务 Skill](.agents/skills/comsol-parameter-task/SKILL.md)。

Agent 接入建议使用 `examples/codex.task.config.toml.example` 配置的受限入口 `src.task_server`：它只开放 `comsol_connect`、按 tag 接管/解除接管模型、切换访问模式、`param_get` 和参数任务工具；不暴露模型全量发现/检查、参数列表、普通 `param_set`、建网格或求解工具。`model_attach` 和 `param_get` 仍可对已知 tag/参数名调用，实际可读范围取决于部署环境权限与专家规则。专家手动使用的完整 MCP 入口仍是 `src.server`。受限入口不会自己启动 COMSOL Server；先在公司环境启动可供多客户端连接的 Server。外部 GPT 不直接连接 MCP，分享哪些模型上下文由专家选择。

1. 专家从 COMSOL Desktop 或内部完整 MCP 取得当前模型的精确 tag，并把 tag 与所选参数信息交给执行 Agent；Agent 只对该 tag 调用 `model_attach`。历史验收使用的临时内存模型已经关闭，不能复用其 tag。
2. 根据真实参数表达式构造任务 JSON，并在 `observe` 下调用 `task_parameter_preview(model_name=<精确 tag>, task=<任务>)`。
3. 专家在本机交互终端运行 `comsol-task-approve <task_id> --data-dir <MCP 数据目录>`，阅读预览并输入指定确认短语。批准 CLI 的 `--data-dir` 必须与 MCP 的 `COMSOL_MCP_DATA_DIR` 相同；Agent 不得代为运行。macOS 与 Windows 的具体命令见[人工验收步骤](docs/task-protocol.md#公司电脑人工验收)。员工助手远程批准接入尚未实现。
4. 专家告知已批准后，Agent 用 `task_parameter_status(task_id)` 确认 `approved`，对同一模型显式设为 `write`，调用 `task_parameter_execute(model_name=<精确 tag>, task=<原任务>)`，然后把外部模型恢复为 `observe`。执行工具会重新核对原值并回读实际表达式。
5. 用 `task_parameter_status(task_id)` 查询本地记录的预览、批准、运行中或终态结果。遇到冲突、报错或回读不一致时停止继续写入，交专家核对真实模型。

参数任务状态使用本地 SQLite 保存，可在 MCP 重启后查询；重启、重连或重新接管模型后，旧预览不得执行，须用新 `task_id` 重新预览与批准。这个 PoC 不自动建网格、求解、保存模型，也尚未实现计算任务排队、自动接续或员工助手集成。真实 COMSOL 验收安排在具备空余 License 的公司电脑上进行。

本机 CLI 与 SQLite 不提供独立身份认证。当前 Codex 本机配置若让 Agent 使用同一账号的任意 shell 或直接写任务数据库，仍可绕过审批；公司正式部署需让 MCP、审批和数据目录处于可信服务边界，并限制 Agent 的任意 shell/数据库访问。员工助手可信审批接口尚待接入。

## 安装条件

- Python 3.10 或更高版本。
- 本机已安装 MPh 支持的 COMSOL Multiphysics。
- 执行真实仿真时具有可用的 COMSOL 许可证。

仓库不包含 COMSOL 软件、许可证、官方手册或用户模型。

## 从零安装

macOS 或 Linux：

```bash
python3 scripts/bootstrap.py
.venv/bin/comsol-mcp-doctor
```

Windows：

```powershell
py scripts/bootstrap.py
.venv\Scripts\comsol-mcp-doctor.exe
```

`bootstrap.py` 默认创建 `.venv`，安装 MCP 核心依赖和开发测试依赖。PDF
知识库是可选功能；确实需要时使用：

```bash
python3 scripts/bootstrap.py --knowledge
```

## 启动 MCP

结构化参数任务使用受限入口：

```bash
.venv/bin/comsol-mcp-task
```

完整工具入口 `.venv/bin/comsol-mcp` 仅供专家手动操作。

## 配置 Codex

Codex 使用 `config.toml` 配置本地 STDIO MCP。参数任务应复制受限的
`examples/codex.task.config.toml.example`，把所有
`/ABSOLUTE/PATH/...` 占位符替换为目标机器上的真实绝对路径，再放入全局
`~/.codex/config.toml` 或受信任项目的 `.codex/config.toml`。

模板中的本地数据目录建议放在代码仓库之外，避免模型、报告和审计记录进入
Git。批准 CLI 需使用相同的 `COMSOL_MCP_DATA_DIR`。配置完成后重启 Codex，并检查 MCP 列表中是否出现 `comsol_tasks`。完整模板 `examples/codex.config.toml.example` 暴露通用修改、网格和求解等工具，仅供专家手动操作。

## Desktop 与 MCP 共用模型

推荐流程：

1. 专家启动多客户端 Server；完整 MCP 的 `comsol_start` 可用于手动操作。受限参数任务入口只通过 `comsol_connect` 连接已运行的 Server。
2. Desktop 通过
   `File > COMSOL Multiphysics Server > Connect to Server`
   连接返回的 `localhost:<port>`。
3. 如果模型原本由 Desktop 持有，先让 Desktop 连接 Server；专家在 Desktop 或内部完整 MCP 中取得精确 tag，执行 Agent 只对该 tag 调用 `model_attach`。
4. 外部模型初始是 `observe`；需要修改时调用
   `model_access_set(..., access_mode="write")`。
5. 修改或求解完成后切回 `observe`，再生成仿真交接报告。
6. MCP 使用 `model_detach` 解除登记，不删除服务器中的外部模型。

COMSOL Server 一次只能处理一个客户端请求。长时间求解期间 Desktop 短暂显示
busy 属于正常现象。

## 验收

普通测试不会启动 COMSOL：

```bash
.venv/bin/pytest
.venv/bin/python -m build
```

真实双客户端验收需要许可证，必须显式执行：

```bash
.venv/bin/pytest -m integration
```

它使用随机临时端口和未保存的内存模型，不连接默认 2036，不扫描用户模型，
结束后关闭临时 Client 和 Server。

## 本地数据边界

以下内容不会进入 Git：

- `.mph` 模型和自动保存文件；
- COMSOL 官方 PDF 手册；
- `.comsol-mcp-data/` 审计记录与集成测试报告；
- `simulation_reports/` 仿真交接报告；
- 向量数据库和模型下载缓存；
- `.env`、`.mcp.json` 和用户实际 Codex 配置；
- 虚拟环境、日志和构建产物。

完整边界见 `RELEASE_CONTENTS.md`。

## 上游来源

本项目基于 MIT 许可的
[`wjc9011/COMSOL_Multiphysics_MCP`](https://github.com/wjc9011/COMSOL_Multiphysics_MCP)
整理和增强。原许可证保留在 `LICENSE`，变更说明见 `NOTICE.md`。
