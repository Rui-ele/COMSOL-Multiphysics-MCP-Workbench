# COMSOL MCP Workbench

GPT 与你讨论技术方案，员工助手中的千问 Agent 执行 COMSOL 任务，你在两边转交完整任务和报告。

**与 GPT 讨论 → 复制任务给员工助手 → 调用 MCP 操作 COMSOL → 报告贴回 GPT → 决定下一步。**

| 角色 | 负责的工作 |
| --- | --- |
| GPT | 查阅资料，确定技术方法、步骤依赖和完成判据，分析返回结果 |
| 员工助手中的 Agent | 连接、定位对象、组织连续工具调用、按条件继续或停止、汇总报告 |
| MCP | 执行具体操作，检查前置条件，回读验证并返回事实 |
| COMSOL | 维护模型状态、建模、计算和生成结果 |

## 能做什么

- 发现并接管 COMSOL Server 上的模型。
- 采集模型概况，读取指定参数、节点属性、实体选择和子树。
- 用通用 API 创建、修改或删除模型节点，并验证实际结果。
- 构建几何与网格，启动后台求解、查询状态，对配置好的结果节点求值或导出。
- 返回可复制的事实报告，长报告通过续页取齐。

GPT 根据当前工具定义和对应版本的 COMSOL 文档编写具体调用；员工助手按任务执行。

## 安装与环境诊断

需要 Python 3.10+、MPh 支持的本机 COMSOL，以及真实操作时可用的 License。

macOS / Linux：

```bash
python3 scripts/bootstrap.py
.venv/bin/comsol-mcp-doctor
```

Windows x64：仓库已带上 Python 3.14 安装程序和完整运行依赖。在仓库根目录执行：

```powershell
powershell -NoProfile -File .\scripts\install_windows.ps1
```

安装入口会复用或安装 Python、建立虚拟环境、从本地安装依赖，并检查 MCP 工具加载。随后按 [Windows 首次初始化任务](docs/initialize-windows.md) 配置 COMSOL 和客户端；该文档可以整段交给员工助手执行，包含缺失材料的下载方法。

`comsol-mcp-doctor` 检查 Python、依赖、COMSOL 安装目录和 Java；运行该命令无需 COMSOL License。初始化生成的 `.comsol-mcp-data/mcp-client.example.json` 包含本机启动路径，可用于不同 MCP 客户端。

运行依赖默认从本地材料安装（Windows）或包索引安装（其他系统）。开发工具使用 `python scripts/bootstrap.py --online --dev`；联网安装可用 `--index-url` 指定公司镜像、用 `--cert` 指定 CA 证书文件。

## 接入员工助手

### MCP 启动信息

本项目提供 stdio MCP 服务，由员工助手侧启动本机进程并连续调用工具。将下列信息交给员工助手的接入配置：

| 配置项 | 内容 |
| --- | --- |
| 传输方式 | `stdio` |
| 启动程序 | 仓库虚拟环境中 Python 的绝对路径；Windows 为 `.venv/Scripts/python.exe`，macOS / Linux 为 `.venv/bin/python` |
| 启动参数 | `-m src.server` |
| 工作目录 | `workbench` 的绝对路径 |
| 环境变量 | `COMSOL_MCP_COMSOL_VERSION`、按需设置 `COMSOL_MCP_COMSOL_ROOT`、`COMSOL_MCP_DATA_DIR`，字段见 [.env.example](.env.example) |

也可以使用虚拟环境内安装的 `comsol-mcp` 命令。员工助手侧的配置字段和本机进程启动能力在公司环境确认。

### 执行规则

把 [AGENTS.md](AGENTS.md) 加入员工助手的执行指令，并加载两份 Skill：

- [信息采集](.agents/skills/comsol-diagnostics/SKILL.md)：选择读取工具、取得完整事实。
- [计划执行](.agents/skills/comsol-model-edit/SKILL.md)：修改、计算、验证和后续步骤衔接。

接入支持 Skill 时按任务加载；使用固定指令时，将这两份简短内容与 AGENTS.md 一起加入执行上下文。

### 连接模型

1. 在工作主机启动支持多客户端的 COMSOL Server。
2. Desktop 通过 `File > COMSOL Multiphysics Server > Connect to Server` 连接该 Server，将本轮使用的模型置于 Server 上。
3. 员工助手调用 `comsol_connect`，用 `model_discover` 找到模型，再用 `model_attach` 接管。
4. 发送任务，在 Desktop 查看模型变化，在员工助手取得报告。

## 日常使用

将 [GPT 项目 instructions](docs/gpt-project-instructions.md) 放入 ChatGPT 项目，然后：

1. 与 GPT 讨论目标、现象和已有事实。
2. GPT 给出本轮任务，包含对象、具体调用、步骤条件、验证方法和返回要求。
3. 整段复制给员工助手执行。转交明确任务即授权执行。
4. 把员工助手汇总的完整报告贴回 GPT，继续分析和安排下一步。

也可以先请员工助手“采集该模型最近求解失败的相关信息，返回一份供 GPT 分析的报告”，再开始讨论。

任务与报告范例、长报告拼接方式见 [任务与报告约定](docs/task-protocol.md)。后台求解返回 `run_id`，后续通过它查询状态；当前取消接口会报告不支持取消。工程判断和下一步技术选择交回 GPT。

## 项目目录

```text
workbench/
├── src/
│   ├── server.py       MCP 启动入口
│   ├── doctor.py       安装环境诊断
│   ├── tools/          Agent 可调用的接口
│   └── core/           会话、API、信息采集、后台求解和报告实现
├── tests/              程序测试；integration/ 保存真实 COMSOL 验收
├── scripts/            安装与发布辅助程序
├── vendor/             随仓库提供的 Windows Python 和依赖安装包
├── docs/               架构、GPT instructions、任务与报告约定
└── .agents/            员工助手使用的执行 Skill
```

模块职责见 [架构说明](docs/comsol-proposal.md)；程序测试、真实 COMSOL 验收和发布内容检查见 [维护与发布说明](RELEASE_CONTENTS.md)。

## 本地数据

- 调用记录写入 `COMSOL_MCP_DATA_DIR/audit/`；未设置时为工作目录下的 `.comsol-mcp-data/audit/`。
- 报告分页缓存在 MCP 进程内，保留最近使用的 16 份；员工助手取齐后汇总成整轮报告。
- 模型保存和结果导出使用任务指定的绝对路径。
- 真实验收报告默认写入数据目录的 `integration_reports/`，也可由验收入口指定路径。

建议把数据目录放在仓库之外。用户按公司要求选择可交给外部 GPT 的信息。

本项目基于 MIT 许可的 [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP) 整理和增强。许可证见 `LICENSE`，来源说明见 [NOTICE.md](NOTICE.md)。
