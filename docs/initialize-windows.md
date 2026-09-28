# Windows 首次初始化任务

把下面整段任务交给员工助手。仓库已带上 Python 安装程序和完整运行依赖；默认从本地安装。

## 可直接转交的任务

请在这台 Windows 公司电脑上完成 COMSOL MCP Workbench 初始化和连接。

### 1. 安装仓库自带的运行环境

找到同时包含 `pyproject.toml`、`scripts/bootstrap.py` 的仓库根目录，在该目录执行：

```powershell
powershell -NoProfile -File .\scripts\install_windows.ps1
```

这个入口会选择已有的标准版 Python 3.14 x64；缺少时使用仓库内的官方安装程序安装到当前用户目录。随后创建项目虚拟环境，从本地安装依赖，检查依赖关系和 MCP 工具加载，并生成 `.comsol-mcp-data/mcp-client.example.json`。

Python 与依赖的来源、版本、文件名和 SHA-256 都在 `vendor/windows-cp314/manifest.json`。材料缺失时脚本会按清单中的官方链接补下载。若下载受公司网络限制，使用公司可用的软件源或下载方式，按清单取得同名文件、核对 SHA-256、放回对应位置，再运行初始化。完整仓库正常安装时无需访问 PyPI。

如果 PowerShell 脚本执行受到限制，使用已有的 Python 3.14 x64 执行等价的安装命令：

```powershell
py -3.14 scripts/bootstrap.py --offline
```

如果同时缺少 Python，先运行 `vendor/windows-cp314/python-3.14.5-amd64.exe`，选择安装到当前用户，再用该 Python 的完整路径执行上面的 `bootstrap.py --offline`。使用带有 `venv` 和 `pip` 的完整 Python 安装。

若已有 `.venv` 使用了其他版本的 Python，另建本项目环境：

```powershell
powershell -NoProfile -File .\scripts\install_windows.ps1 -Venv .venv-win314
```

后续使用初始化输出的 Python 路径和客户端配置。

### 2. 找到本机 COMSOL

优先检查用户提供的安装目录、注册表安装信息和 `where.exe comsol`。核实实际目录中存在 `bin\win64\comsol.exe`、`plugins`、`apiplugins`，并找到 COMSOL 配套的 `java\win64\jre`。多个版本时说明候选并让用户选择。

COMSOL 软件和许可证由公司提供。尚未安装时，向用户取得公司软件中心入口或已有安装包位置、所需版本和许可证配置，使用该来源完成安装。优先复用已有安装。

确认安装位置后，在本次进程设置：

```powershell
$comsolRoot = '<实际的 Multiphysics 目录>'
$comsolVersion = '<所选版本，例如 6.4>'
$env:COMSOL_MCP_COMSOL_ROOT = $comsolRoot
$env:COMSOL_MCP_COMSOL_VERSION = $comsolVersion
$env:JAVA_HOME = Join-Path $comsolRoot 'java\win64\jre'
$env:PATH = (Join-Path $comsolRoot 'bin\win64') + ';' + $env:PATH
```

从初始化生成的配置中取得 Python 路径，运行：

```powershell
$config = Get-Content .\.comsol-mcp-data\mcp-client.example.json -Raw | ConvertFrom-Json
$python = $config.mcpServers.comsol.command
& $python -m src.doctor --version $comsolVersion --json
```

`COMSOL_MCP_COMSOL_ROOT` 用于项目路径检查；将 COMSOL 的可执行文件目录加入 MCP 进程的 `PATH`，让 MPh 实际发现同一个安装位置。配置只需作用于项目进程。

### 3. 接入当前 MCP 客户端

以 `.comsol-mcp-data/mcp-client.example.json` 为基础，按当前客户端的配置格式填写启动程序、参数和工作目录。把上一步的 `COMSOL_MCP_COMSOL_ROOT`、`COMSOL_MCP_COMSOL_VERSION`、`JAVA_HOME` 和完整 `PATH` 加入该 MCP 进程的环境变量。生成的是通用参考格式，字段以客户端实际支持的格式为准。

配置应写入客户端实际使用的位置；`.env` 本身不会被当前 MCP 入口自动加载。若客户端不支持工作目录字段，可将启动参数改为：

```json
["-c", "import os, runpy, sys; os.chdir(sys.argv[1]); runpy.run_module('src.server', run_name='__main__')", "<仓库绝对路径>"]
```

重启该 MCP 连接，从客户端取得实际工具列表，调用 `comsol_status`。已有 COMSOL Server 时用它的真实地址和端口调用 `comsol_connect`；需要新开会话时调用 `comsol_start`，明确传入所选 `version`。连接后调用 `model_discover`，确认能够读取 Server 的模型列表。空列表也可以证明读取成功。

### 4. 返回一份初始化结果

请集中返回：

- Python 路径与版本、最终依赖版本清单（用项目 Python 执行 `-m pip list --format=json`）。
- COMSOL 版本与路径、客户端实际使用的启动配置；隐藏凭据。
- 安装检查、MCP 工具调用、COMSOL 连接及模型发现各自的结果。
- 未完成步骤的命令、原始输出或工具返回，以及下一步所缺的信息。
- 创建或修改的本地配置文件路径。

把完整结果保存在 `.comsol-mcp-data/install-report.md`，并在对话中提供可整段复制的报告。

## 安装范围

随仓库提供的材料针对 **Windows x64、标准 CPython 3.14**。安装程序为 Python 3.14.5，已有兼容的 3.14.x 可直接复用。其他系统使用 README 中的联网安装入口；其他 Python 主次版本需要对应的依赖包。

这份材料已核对官方校验值、Windows 目标依赖关系和安装计划。Windows 上的实际安装、客户端连接与 COMSOL 连接由本任务验收。
