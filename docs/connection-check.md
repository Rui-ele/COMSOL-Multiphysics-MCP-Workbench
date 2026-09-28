# MCP 通信与 COMSOL 连接自检

`scripts/check_connection.py` 使用官方 MCP SDK，检查真实 stdio 工具调用。报告记录被测源码、Python 与依赖版本、各步耗时、原始返回和服务端错误日志，便于在不同电脑或代码版本之间比较。

脚本使用已有 Python 环境。连接测试只读取会话状态和模型列表，不修改模型。安装入口会自动运行基础 MCP 通信自检；COMSOL 安装发现与 Server 连接可按下面的步骤单独验证。

## Windows CMD：更新并检查新版

以下命令在 **CMD（命令提示符）** 中执行，公司电脑示例目录为 `E:\COMSOL-MCP\workbench-clean-test`。逐条执行，某条失败时把输出交给员工助手。

```cmd
cd /d E:\COMSOL-MCP\workbench-clean-test
git pull --ff-only
.venv\Scripts\python.exe scripts\check_connection.py --mode mcp
```

这一模式检查 `initialize → tools/list → comsol_status`，会正常加载项目及 MPh Python 模块，不启动 COMSOL 或 JVM。尚未连接时，`connected: false` 是正常结果；本步检查的是工具能否返回有效响应。

### 检查安装发现

把 `--comsol-root` 指向实际的 `Multiphysics` 目录：

```cmd
.venv\Scripts\python.exe scripts\check_connection.py --mode discovery --comsol-root "E:\COMSOL\6.4\COMSOL64\Multiphysics"
```

`--comsol-root` 在被测进程中设置 `COMSOL_MCP_COMSOL_ROOT`，并将安装下的可执行文件目录放在该进程 `PATH` 的最前面。`JAVA_HOME`、COMSOL 版本等环境值沿用当前环境。脚本将本次测试数据写入报告目录，并使用 UTF-8 输出；报告会保留实际环境、MPh 的发现结果与日志。

### 连接已有 Server

确认 COMSOL Server 已在 `localhost:2036` 监听，再执行：

```cmd
.venv\Scripts\python.exe scripts\check_connection.py --mode connect --host localhost --port 2036 --comsol-root "E:\COMSOL\6.4\COMSOL64\Multiphysics"
```

这一模式依次检查基础 MCP 调用、MPh 安装发现、`comsol_connect`、连接后的 `comsol_status` 和 `model_discover`。Server 没有模型时，成功返回空列表也算通过。端口或主机不同，在命令中替换相应值。

脚本结束时会断开测试客户端，不发送停止已有 Server 的命令。单客户端 Server 可能在客户端断开时自行退出；需要持续运行并供 Desktop、员工助手共用时，以 `-multi on` 启动：

```cmd
"E:\COMSOL\6.4\COMSOL64\Multiphysics\bin\win64\comsolmphserver.exe" -port 2036 -multi on
```

每次运行结束时，终端会给出 Markdown、JSON 和错误日志的位置。默认保存到被测仓库的 `.comsol-mcp-data/connection-check/`。把 Markdown 报告整段交给 GPT，需要进一步定位时再查看 JSON 和日志。

## 用同一个脚本比较旧版

保留上面的新版工作目录，指定旧版源码目录和同一个 Python：

```cmd
.venv\Scripts\python.exe scripts\check_connection.py --repo "E:\COMSOL-MCP\workbench-old-f1f551a" --python "E:\COMSOL-MCP\workbench-clean-test\.venv\Scripts\python.exe" --mode mcp
```

旧版连接测试：

```cmd
.venv\Scripts\python.exe scripts\check_connection.py --repo "E:\COMSOL-MCP\workbench-old-f1f551a" --python "E:\COMSOL-MCP\workbench-clean-test\.venv\Scripts\python.exe" --mode connect --host localhost --port 2036 --comsol-root "E:\COMSOL\6.4\COMSOL64\Multiphysics"
```

脚本在指定源码目录启动 MCP 服务。报告中的源码路径、Git 提交与依赖版本用于确认比较条件；旧版目录可以固定在 `f1f551a`。

## 给员工助手的任务

> 请按本文件运行新版的 MCP 通信自检，再检查本机 COMSOL 安装发现和已有 Server 连接。使用项目现有 Python 环境和 `scripts/check_connection.py`。若新版基础调用失败，用同一个脚本和 Python 测试旧版目录，汇总两份报告。逐项说明通信、安装发现、连接、模型发现的结果，附报告路径和首个失败步骤的原始错误。保留现有依赖版本与模型状态；需要新的排查方案时，把事实带回 GPT。

## 结果如何使用

| 结果 | 下一步 |
| --- | --- |
| 基础 MCP 调用失败 | 比较同一环境下新旧源码的结果，从首个失败步骤继续定位 |
| 基础调用通过、安装发现失败 | 根据 MPh 日志核对安装路径和客户端组件 |
| 发现安装通过、连接失败 | 核对已有 Server 的地址、端口及错误原文 |
| 连接与模型发现通过 | 将相同 Python、工作目录和环境配置到员工助手，再从员工助手调用工具 |

自检通过说明脚本客户端的调用链可用。员工助手实际使用的 MCP 配置仍需验证：取得工具列表、调用状态查询、连接同一 Server 并发现模型。模型编辑与求解属于下一层验收，见 [维护与发布说明](../RELEASE_CONTENTS.md)。

## 其他系统与参数

macOS / Linux 在仓库目录运行：

```bash
.venv/bin/python scripts/check_connection.py --mode mcp
```

| 参数 | 默认值与用途 |
| --- | --- |
| `--repo` | 脚本所在仓库；可指定另一份源码 |
| `--python` | 运行脚本的 Python；用于启动被测进程 |
| `--mode` | `mcp`；也可选 `discovery` 或 `connect` |
| `--host` / `--port` | `localhost` / `2036`，连接已有 Server |
| `--comsol-root` | 可选；本次测试使用的 COMSOL 安装目录 |
| `--report-dir` | 被测仓库下的 `.comsol-mcp-data/connection-check/` |
| `--timeout` | 普通请求 30 秒 |
| `--connect-timeout` | 连接请求 90 秒 |
