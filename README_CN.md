# COMSOL MCP Workbench

这是一套可独立安装、便于复用的本地 COMSOL MCP。它允许 MCP 客户端和
COMSOL Desktop 连接同一个多客户端 COMSOL Server，并共同操作服务器内存中的
同一模型。

## 主要能力

- 发现并按稳定 tag 接管 Desktop 或其他客户端已经持有的模型。
- 外部模型默认进入只读 `observe` 模式。
- 只有显式切换到 `write` 后才能修改参数、重建或求解。
- 即使进入 `write`，MCP 也不能直接保存或删除外部模型。
- 记录精简审计日志并生成中文仿真交接报告。
- 提供真实双客户端验收脚本，验证跨客户端读写与生命周期保护。

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

```bash
.venv/bin/comsol-mcp
```

也可以继续使用：

```bash
.venv/bin/python -m src.server
```

## 配置 Codex

Codex 使用 `config.toml` 配置本地 STDIO MCP。复制
`examples/codex.config.toml.example` 中的内容，把所有
`/ABSOLUTE/PATH/...` 占位符替换为目标机器上的真实绝对路径，再放入全局
`~/.codex/config.toml` 或受信任项目的 `.codex/config.toml`。

模板中的本地数据目录建议放在代码仓库之外，避免模型、报告和审计记录进入
Git。配置完成后重启 Codex，并检查 MCP 列表中是否出现 `comsol`。

## Desktop 与 MCP 共用模型

推荐流程：

1. MCP 调用 `comsol_start`，启动多客户端 Server。
2. Desktop 通过
   `File > COMSOL Multiphysics Server > Connect to Server`
   连接返回的 `localhost:<port>`。
3. 如果模型原本由 Desktop 持有，先让 Desktop 连接 Server，再由 MCP 调用
   `model_discover` 和 `model_attach`。
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
