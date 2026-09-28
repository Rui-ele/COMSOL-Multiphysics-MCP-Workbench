# COMSOL MCP 排错定位与交接说明

整理日期：2026-09-28。运行代码基准：`6eb7e9488afd18b00436bf51d17c432afd0b9ae8`（简称 `6eb7e94`）。本次交接补充文档和证据摘要，程序沿用该版本。

## 1. 一分钟看懂当前结果

**公司 Windows 电脑上，直接用 Python 连接 COMSOL 已成功；通过 MCP 的 stdio 调用连接工具，仍会超时。** 目前已把故障缩小到这个执行环境差异，具体是哪一个底层环节阻塞，还需一次对照测试。

已得到的关键结果：

- 裸 `mph.Client(...)`：约 **1.35 秒**连接成功，随后正常断开。
- 项目自己的 `session_manager.connect(...)`：约 **1.33 秒**成功，随后正常断开。
- MCP stdio：状态查询和安装发现通过，`comsol_connect` 等待 **90 秒**仍未返回。
- 把同一个 MCP 服务改用 SSE 的对照测试，**尚未执行出结果**。

接手人可以从“为什么进入 MCP stdio 环境后才挂起”继续查。下一步 SSE 对照用于区分通信方式的影响和更广的 MCP 执行环境影响。

交接包保留的是当前代码与排查成果。Windows 的“员工助手 → MCP → COMSOL”完整连接链尚待打通。

## 2. 这些软件分别做什么

```text
员工助手 / 自检客户端
  │  发送“连接 COMSOL”等请求
  ▼
MCP 通信层（当前用 stdio）
  │  把请求交给项目工具
  ▼
comsol_connect → session_manager.connect
  │  调用 Python 连接库
  ▼
MPh → JPype / JVM → COMSOL Java API
  │  连接 localhost:2036
  ▼
COMSOL Multiphysics Server
```

| 名称 | 通俗解释 | 本次排查的作用 |
| --- | --- | --- |
| MCP | 让员工助手调用项目工具的通信规范 | 工具列表能显示，不等于每个工具都能执行完成 |
| stdio | 通过进程的标准输入、输出传递 MCP 消息 | 当前 Windows 连接超时发生在这条运行路径 |
| SSE | 通过本机 HTTP 连接传递 MCP 消息的另一种方式 | 用来比较更换通信方式后，同一个工具是否恢复 |
| 项目连接层 | `session_manager.connect`，负责准备环境并连接 | 在普通 Python 进程里单独调用已成功 |
| MPh | Python 调用 COMSOL 的库 | 单独连接已成功 |
| JPype / JVM | 让 Python 使用 Java；JVM 是 Java 的运行环境 | 位于 MPh 与 COMSOL API 之间 |
| COMSOL Server | 实际承载模型和计算的服务进程 | 测试中监听本机 2036 端口 |

裸 MPh 测试绕过了 MCP 和项目连接层；直接调用 `session_manager` 的测试只绕过 MCP。两条都成功，所以这两次对照把排查重点移到了 MCP 运行环境。

## 3. 测试结果对照

| 平台 / 版本 | 测试内容 | 实际结果 | 能说明什么 |
| --- | --- | --- | --- |
| Windows，旧版 `f1f551a` | MCP stdio 状态查询 | 通过 | 同一 Python 环境可以运行旧版基础工具 |
| Windows，新版 `6d84e86` | MCP stdio 状态查询 | 超时 | 新旧代码在基础工具调用上出现差异 |
| Windows，修正后 `6eb7e94` | MCP stdio 状态查询、安装发现 | 通过 | 第一阶段问题已有改善，能找到 COMSOL 安装 |
| Windows，`6eb7e94` | MCP stdio `comsol_connect` | 90 秒超时 | 当前阻塞仍在连接调用阶段 |
| Windows，同环境 | 裸 `mph.Client` | 1.35 秒成功，断开成功 | 此环境下 Python 可以直接建立 COMSOL 连接 |
| Windows，`6eb7e94` | 直接 `session_manager.connect` | 1.33 秒成功，断开成功 | 当前项目连接函数在普通 Python 进程里可以完成 |
| Windows，同一服务改用 SSE | `comsol_connect`、`model_discover` | 待验证 | 尚不能判断故障是否只随 stdio 出现 |
| Mac，已完成的项目验收 | 真实 stdio 连接、模型发现与接管 | 通过 | Mac 的实际工具链已跑通 |
| Mac，已完成的项目验收 | 参数从 1 改为 42，两个客户端回读；保存；热模型 350 K 求解 | 通过 | Mac 已验证模型操作与计算链路 |
| Mac，已完成的项目验收 | 程序测试 | 82 项通过 | 自动测试已有通过记录 |

Mac 和 Windows 的系统及 Python 版本不同，Mac 结果不能替代 Windows 验收。上述直连成功也只覆盖已执行的连接与断开操作。

## 4. 两阶段问题，以及代码已经修正的内容

### 第一阶段：连状态查询也不返回

同一台 Windows、同一个 Python 下，旧版 `f1f551a` 的状态查询通过，新版 `6d84e86` 超时。检查发现，新版工具生成报告时会启动 Git 子进程读取版本信息。

`6eb7e94` 做了两项修正：

1. 工具报告读取本地版本元数据，移除了工具调用路径上的 Git 子进程。
2. 连接前真正应用 `COMSOL_MCP_COMSOL_ROOT` 和 `COMSOL_MCP_COMSOL_VERSION`：把指定安装的可执行文件目录放入 `PATH`，并将版本传给 MPh。

更新后，Windows 的状态查询、安装发现通过。当前保留了修正前后的结果，尚未取得第一次挂起时的底层调用栈。

### 第二阶段：状态正常，但连接 COMSOL 仍然超时

`6eb7e94` 的 stdio 连接测试仍在 `comsol_connect` 等待约 90 秒。COMSOL Server 在超时清理附近记录了“客户端登录”，随后记录“客户端断开”。

之后的两个普通 Python 测试分别在 1.35 秒和 1.33 秒成功，说明测试时服务启动、端口访问、账号登录和 Java 客户端的基本连接可用。后续排查重点是 MCP 执行环境中的差异。

Server 的登录时刻与测试清理接近，是值得保留的线索。但仅凭这段时间线，还不能证明具体卡在输入输出、线程、事件循环或 Java 调用的哪一步。

## 5. 已知环境

| 项目 | 公司 Windows | Mac 已通过的环境 |
| --- | --- | --- |
| 系统 / 硬件 | Windows 11，内部版本 10.0.26100.6584 | macOS，Apple Silicon |
| Python | 3.14.5 | 3.10.2 |
| MCP Python SDK | 1.30.0 | 1.30.0 |
| AnyIO | 4.15.1 | 4.15.1 |
| MPh | 1.4.0 | 1.4.0 |
| JPype1 | 1.7.1 | 1.7.1 |
| COMSOL | 6.4，开发版本 343 | 6.4 |
| 本次 Windows 测试目录 | `E:\COMSOL-MCP\workbench-clean-test` | — |
| COMSOL 安装目录 | `E:\COMSOL\6.4\COMSOL64\Multiphysics` | — |
| 外部 Server | `localhost:2036`，`-multi on` | 见原验收记录 |

完整的 Windows Python 包版本见证据摘要；JVM 具体版本可在后续实验报告中补录。

## 6. 关键输出与证据入口

下列为用户在 Windows CMD 中贴回的输出摘录。结构化汇总见 [交接证据记录](handoff-evidence.json)。

裸 MPh：

```text
BEFORE CONNECT
CONNECTED 1.35 s 6.4 2036
DISCONNECTED
```

直接项目连接层：

```text
BEFORE
AFTER 1.33 s
{'success': True, 'version': '6.4', 'cores': 10, 'standalone': False, 'session_mode': 'shared-external', 'server_host': 'localhost', 'server_port': 2036, 'server_managed': False, 'desktop_connection': {'host': 'localhost', 'port': 2036, 'connect_menu': 'File > COMSOL Multiphysics Server > Connect to Server', 'import_menu': 'File > COMSOL Multiphysics Server > Import Application from Server', 'usage': 'Connect COMSOL Desktop to view the same Server model.'}}
{'success': True, 'completed_stages': ['disconnect_client'], 'message': 'Disconnected from external COMSOL server; server left running.'}
```

连接失败报告为 `20260928_172746_connect_940930b5`。报告记录 `comsol_connect` 等待 **90.003 秒**，没有收到工具返回，错误为 `TimeoutError`；后续关闭通信也失败。服务端日志最后停在：

```text
INFO:mph:Starting Java virtual machine.
INFO:mph:Java virtual machine has started.
INFO:mph:Connecting to server "localhost" at port 2036.
```

Server 窗口显示开始监听 2036；在 17:29:19 记录 API 客户端登录并断开。对应的 stdio 测试约在 17:27:47 开始、17:29:17 超时。这条时间线是后续定位阻塞位置的线索。

现有自检会在 `.comsol-mcp-data/connection-check/` 生成 Markdown、JSON 和服务端错误日志。该目录属于本机运行数据；若交接包没有某次原始日志，以上摘录和证据汇总就是本包对应的记录范围。

## 7. 接手后的最小对照测试

目标：固定代码、Python、COMSOL Server 和工具，仅更换 MCP 通信方式，观察连接是否恢复。以下是待执行方案，交接时尚无 SSE 结果。

### 准备：分开 Server 窗口与测试窗口

**CMD 窗口 A** 只运行 Server，并保持打开：

```cmd
"E:\COMSOL\6.4\COMSOL64\Multiphysics\bin\win64\comsolmphserver.exe" -port 2036 -multi on
```

**CMD 窗口 B** 进入项目并设置环境。新下载的文件夹名称或 COMSOL 位置不同，替换成实际路径：

```cmd
cd /d E:\COMSOL-MCP\workbench-clean-test
set "COMSOL_MCP_COMSOL_ROOT=E:\COMSOL\6.4\COMSOL64\Multiphysics"
set "COMSOL_MCP_COMSOL_VERSION=6.4"
set "PATH=E:\COMSOL\6.4\COMSOL64\Multiphysics\bin\win64;%PATH%"
```

Server 运行后占用了窗口 A；在其中输入 `set` 不会设置另一个测试进程的环境变量。

### 如需重现已知的两个成功结果

在窗口 B 逐条执行，每条都启动独立 Python 进程：

```cmd
.venv\Scripts\python.exe -c "import mph,time; print('BEFORE CONNECT'); t=time.time(); c=mph.Client(version='6.4',host='localhost',port=2036); print('CONNECTED',round(time.time()-t,2),'s',c.version,c.port); c.disconnect(); print('DISCONNECTED')"
.venv\Scripts\python.exe -c "import time; from src.core.session import session_manager; print('BEFORE'); t=time.time(); r=session_manager.connect(port=2036,host='localhost'); print('AFTER',round(time.time()-t,2),'s'); print(r); print(session_manager.disconnect())"
```

### 只改变 MCP transport 的对照要求

1. 固定 `6eb7e94` 的运行代码和当前 `.venv`；保留同一个窗口 A 的 Server。
2. 用现有自检记录 stdio 结果，普通请求和连接请求均设为 30 秒：

   ```cmd
   .venv\Scripts\python.exe scripts\check_connection.py --mode connect --host localhost --port 2036 --comsol-root "E:\COMSOL\6.4\COMSOL64\Multiphysics" --timeout 30 --connect-timeout 30
   ```

3. 另启同一份 `src.server.mcp`，先调用 `register_all_tools()` 注册原有工具，再运行 SSE。窗口 B 可以直接使用下面的临时启动命令；默认监听本机 8000 端口：

   ```cmd
   .venv\Scripts\python.exe -c "from src.server import mcp,register_all_tools; register_all_tools(); mcp.run(transport='sse')"
   ```

4. 在窗口 C 用同一个 Python 的 MCP SDK SSE 客户端连接 `http://127.0.0.1:8000/sse`，初始化后依次调用 `comsol_status`、`comsol_connect(host="localhost", port=2036)`、`comsol_status`、`model_discover`。每个请求设 30 秒超时；保存原始返回、耗时及窗口 B 的日志。现有 `check_connection.py` 只测试 stdio，SSE 客户端可单独放在临时测试目录。
5. 复用项目已注册的工具；本次连接使用外部 Server，不调用 `comsol_start`。成功后用 `comsol_disconnect` 断开测试客户端，窗口 A 保持运行。

| 对照结果 | 接下来可以得出的结论 |
| --- | --- |
| SSE 连接成功，stdio 仍超时 | 在这组固定条件下，故障只随 stdio 路径出现；可进一步检查 stdio 运行环境，并评估 SSE 接入 |
| SSE 连接仍超时 | 故障范围扩大到两种 MCP 执行环境；保留首个失败步骤与日志后重新判断 |
| SSE 连接成功，模型发现失败 | 连接环节已通过；模型发现需按它自己的错误继续定位 |
| SSE 返回成功且模型列表为空 | Server 没有模型时属于有效结果 |
| 两种方式都成功 | 本轮没有复现；比较进程状态、环境变量和原超时报告中的条件 |

下一份报告只需清楚回答：SSE 连接是否成功、耗时多少；模型发现是否成功；与 stdio 相比变化在哪里。

## 8. 相关文件

| 文件 | 用途 |
| --- | --- |
| [src/server.py](../src/server.py) | MCP 实例、工具注册和默认启动入口 |
| [src/tools/session.py](../src/tools/session.py) | `comsol_connect`、状态与断开工具 |
| [src/core/session.py](../src/core/session.py) | 连接、断开、会话状态实现 |
| [src/core/comsol_environment.py](../src/core/comsol_environment.py) | 应用安装目录、版本和 MPh 发现缓存 |
| [src/core/runtime_info.py](../src/core/runtime_info.py) | 报告版本信息；已移除 Git 子进程 |
| [src/core/tool_runtime.py](../src/core/tool_runtime.py) | 工具执行与审计包装 |
| [scripts/check_connection.py](../scripts/check_connection.py) | 真实 stdio 自检、超时监督和报告输出 |
| [连接自检说明](connection-check.md) | 自检参数、旧版对照与报告位置 |
| [Windows 初始化](initialize-windows.md) | 新机器的安装步骤 |

证据来源为公司电脑转交的 CMD 输出、自检报告及本地 Mac 验收记录，关键结果已收录在本文和证据摘要中。接手人可以从第 7 节继续。

## 9. 转交文案

COMSOL MCP 项目代码和排错记录已整理好。当前版本已在 MacBook 上验证连接 COMSOL、参数修改回读、模型保存及简单模型建模求解。公司 Windows 端直接连接 COMSOL 已成功，通过 MCP 接入时仍存在连接超时；问题范围、测试结果和下一步排查方法已写入交接文档，方便后续继续接入。
