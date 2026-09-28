# 维护与发布说明

## 验证入口

| 用途 | 入口 | 使用时机 |
| --- | --- | --- |
| 安装环境诊断 | `comsol-mcp-doctor` | 首次安装或排查启动问题 |
| 程序测试 | `python -m pytest` | 修改代码后，默认跳过需要 COMSOL 的真实验收 |
| 真实 COMSOL 验收 | `python -m pytest -m integration tests/integration` | 在有 COMSOL 和可用 License 的公司电脑上 |
| 发布内容检查 | `python scripts/release_audit.py` | 发布源码或构建安装包前 |

真实验收启动临时 Server 和两个客户端，验证模型发现、接管、参数修改与回读、保存副本、解除登记、断开和删除。场景实现集中在 `tests/integration/handoff_runner.py`；需要保留 JSON 和 Markdown 验收记录时，可以直接运行：

```bash
python -m tests.integration.handoff_runner --report-dir /ABSOLUTE/PATH/TO/acceptance-reports
```

两种运行方式使用同一场景实现。通过员工助手完成 GPT 任务往返、分页和求解的人工验收，见 [公司电脑人工验收](docs/task-protocol.md#公司电脑人工验收)。

## 发布内容

- `src/`：MCP 接口、连接与模型管理、通用 API、计算和文件操作、事实报告、安装环境诊断。
- `AGENTS.md`、`.agents/skills/`、`docs/`：员工助手执行规则、Skill、GPT instructions 和交接约定。
- `tests/`：程序测试和真实 COMSOL 验收。
- `scripts/`：安装与发布辅助程序。
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
