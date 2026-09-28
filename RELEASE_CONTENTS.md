# 维护与发布说明

## 验证入口

| 用途 | 入口 | 使用时机 |
| --- | --- | --- |
| 安装环境诊断 | `comsol-mcp-doctor` | 首次安装或排查启动问题 |
| MCP 通信自检 | `python scripts/check_connection.py --mode mcp` | 检查真实 stdio 握手、工具列表和状态调用，无需启动 COMSOL |
| 已有 Server 连接自检 | `python scripts/check_connection.py --mode connect` | 验证连接已有 COMSOL Server、读取状态和发现模型 |
| 程序测试 | `python -m pytest` | 修改代码后，默认跳过需要 COMSOL 的真实验收 |
| 双客户端真实验收 | `python -m pytest -m integration tests/integration` | 在有 COMSOL 和可用 License 的电脑上验证模型交接 |
| 发布内容检查 | `python scripts/release_audit.py` | 发布源码或构建安装包前 |

双客户端验收启动临时 Server 和两个客户端，验证模型发现、接管、参数修改与回读、保存副本、解除登记、断开和删除。场景实现集中在 `tests/integration/handoff_runner.py`；需要保留 JSON 和 Markdown 验收记录时，可以直接运行：

```bash
python -m tests.integration.handoff_runner --report-dir /ABSOLUTE/PATH/TO/acceptance-reports
```

上述两种运行方式使用同一场景实现。

热模型验收是独立入口，建立一维导热模型，验证几何、网格、求解和中点温度。它使用临时 Server，需要 COMSOL 和可用 License；单独运行：

```bash
python -m tests.integration.thermal_runner --report-dir /ABSOLUTE/PATH/TO/acceptance-reports
```

`pytest -m integration` 当前运行双客户端场景，热模型通过上述命令执行。通过员工助手完成 GPT 任务往返、分页和求解的人工验收，见 [公司电脑人工验收](docs/task-protocol.md#公司电脑人工验收)。

## 已验证范围

2026-09-28 在 Apple Silicon Mac、Python 3.10.2、MCP 1.30.0、MPh 1.4.0 和 COMSOL 6.4 上完成真实 stdio 验收：状态与连接、模型交接、参数修改回读、保存副本及模型生命周期均通过；独立导热场景完成建模、网格与求解，中点温度约为 350 K。

本轮同时修正了共享模型删除后的状态反馈：其他客户端仍使用模型时，COMSOL 保留 Server 模型；最后一个使用者删除后才核验 tag 消失。公司 Windows 已通过新版 MCP 通信和安装发现检查，直接 Python 连接 COMSOL 也成功，MCP stdio 下的连接仍超时。原始报告和机器环境记录在本地保存。

### Windows 连接修正

针对同一 Windows 环境中旧版状态调用通过、新版超时的结果，本版移除了工具报告中的 Git 子进程和工作区扫描。报告直接读取本地版本文件，工作区是否有改动由独立自检报告提供。

连接和启动现在实际使用 `COMSOL_MCP_COMSOL_ROOT` 与 `COMSOL_MCP_COMSOL_VERSION`。这使员工助手与自检使用相同的安装配置。修正后在 Mac 重新验证了连接、参数写入回读、保存及导热求解；公司 Windows 的结果以实际调用为准。

### 交接记录

运行代码以 `6eb7e94` 为交接基准，后续交接提交仅补充文档和证据摘要。Windows 的最新对照结果、已修正事项和下一步最小实验见 [排错定位与交接](docs/troubleshooting-handoff.md)，机器可读摘要见 [验收与排错证据](docs/handoff-evidence.json)。

## 发布内容

- `src/`：MCP 接口、连接与模型管理、通用 API、计算和文件操作、事实报告、安装环境诊断。
- `AGENTS.md`、`.agents/skills/`、`docs/`：员工助手执行规则、Skill、GPT instructions 和交接约定。
- `tests/`：程序测试和真实 COMSOL 验收。
- `scripts/`：安装、通信与连接自检、发布辅助程序。
- `vendor/windows-cp314/`：随仓库及源码包分发的 Windows Python 安装程序、运行依赖、版本与校验清单。
- 项目配置、中英文 README、许可证和上游来源说明。

源码包包含以上维护文件；wheel 安装运行代码和命令入口。员工助手所需的协作资料随仓库或源码包提供。

模型、COMSOL 软件与许可证、官方手册、日志、运行报告、缓存和机器配置在本地保存。发布检查会检查这类文件、过大的文件和开发机器路径。vendor 中清单登记的安装材料按 SHA-256 核对后允许分发。

## 构建安装包

在项目虚拟环境中运行：

```bash
python scripts/release_audit.py
python -m build
```

本项目当前版本由 `pyproject.toml` 和 `src/__init__.py` 定义。分发 `dist/` 中与当前版本对应的包；旧版本包在重新构建时清理。
