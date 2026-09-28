# COMSOL MCP Workbench

GPT plans COMSOL tasks with the user. A Qwen Agent in the company's Employee
Assistant executes them through MCP. The user transfers complete tasks and reports.

**Discuss with GPT → copy a task to Employee Assistant → execute through MCP → paste the report back to GPT → plan the next step.**

[中文使用说明](README_CN.md)

## Responsibilities

| Role | Work |
| --- | --- |
| GPT | Consult documentation, choose technical methods, define dependencies and verification criteria, interpret results |
| Employee Assistant Agent | Connect, locate objects, coordinate tool calls, follow task conditions, assemble reports |
| MCP | Execute explicit operations, check preconditions, read back state and return facts |
| COMSOL | Maintain models, build, solve and produce results |

The tools cover model discovery and attachment, scoped inspection, generic API
reads and verified changes, geometry and mesh builds, background solving,
configured result evaluation, and file operations. GPT supplies concrete calls
from the deployed tool definitions and matching COMSOL documentation.

## Install and diagnose the installation

Requires Python 3.10+, a local COMSOL installation supported by MPh, and an
available license for live model operations.

macOS / Linux:

```bash
python3 scripts/bootstrap.py
.venv/bin/comsol-mcp-doctor
```

If the Python-supplied virtual environment has an older pip that cannot install
this `pyproject.toml` project in editable mode, rerun bootstrap with
`--upgrade-pip`. Python 3.10.2 with pip 21.2.4 requires this on macOS.

Windows x64: the repository includes a Python 3.14 installer and the complete runtime dependency bundle. From the repository root:

```cmd
powershell -NoProfile -File .\scripts\install_windows.ps1
```

The installer reuses or installs Python, creates the virtual environment, installs
the local wheels, checks dependencies, writes client configuration and runs a real
MCP communication check. Follow
the [Windows initialization task](docs/initialize-windows.md) to configure COMSOL
and your MCP client. It can be handed directly to Employee Assistant and includes
instructions for obtaining missing installation materials.

The generated `.comsol-mcp-data/mcp-client.example.json` contains the actual local
launch paths for a stdio MCP client. `comsol-mcp-doctor` checks Python, packages,
COMSOL paths and Java without consuming a COMSOL license.

Development dependencies are optional: `python scripts/bootstrap.py --online --dev`.
Online installation supports `--index-url` for a company mirror and `--cert` for
a CA certificate bundle. Windows defaults to the local runtime bundle; other
platforms use online installation.

### Check real MCP communication

Installation runs this check automatically. To repeat it after updating the code,
run this from the repository directory in Windows CMD:

```cmd
.venv\Scripts\python.exe scripts\check_connection.py --mode mcp
```

The official MCP SDK initializes the server, lists tools and calls `comsol_status`;
COMSOL does not need to be running. Reports under
`.comsol-mcp-data/connection-check/` record versions, timings, raw responses and
stderr. On macOS / Linux, use `.venv/bin/python`.

See [connection checks](docs/connection-check.md) for COMSOL installation
discovery, connecting to an existing Server, and comparing old and new source
with the same Python environment. After this check passes, verify the Employee
Assistant's actual MCP connection using the same launch environment.

## Connect Employee Assistant

The MCP server uses stdio. The Employee Assistant integration must launch a
local process and support consecutive tool calls. Supply these launch settings:

| Setting | Value |
| --- | --- |
| Transport | `stdio` |
| Program | Absolute path to `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on macOS / Linux |
| Arguments | `-m src.server` |
| Working directory | Absolute path to `workbench` |
| Environment | `COMSOL_MCP_COMSOL_VERSION`; optionally `COMSOL_MCP_COMSOL_ROOT` and `COMSOL_MCP_DATA_DIR`; see [.env.example](.env.example) |

The installed `comsol-mcp` executable is an equivalent entry point. Confirm the
Employee Assistant's configuration fields and local process support in the
company environment.

Load [AGENTS.md](AGENTS.md) as the execution instructions, along with the
[diagnostics Skill](.agents/skills/comsol-diagnostics/SKILL.md) and
[execution Skill](.agents/skills/comsol-model-edit/SKILL.md). Load Skills as needed
when supported, or include both short Skills in the execution instructions.

Start a multi-client COMSOL Server and connect Desktop to it. Employee Assistant
then calls `comsol_connect`, `model_discover`, and `model_attach` to work with the
intended Server model.

## Daily use

1. Set up the [GPT project instructions](docs/gpt-project-instructions.md).
2. Discuss the problem with GPT and obtain a task with explicit operations,
   dependencies, verification criteria and required evidence.
3. Copy the task to Employee Assistant. Forwarding an explicit task authorizes execution.
4. Paste its complete report back to GPT for analysis and the next task.

You can also start by asking Employee Assistant to collect a model overview for
GPT. See [task/report examples and pagination](docs/task-protocol.md).
Background solving returns a `run_id` for subsequent status queries. The current
cancel endpoint reports cancellation as unsupported.

## Layout

```text
workbench/
├── src/
│   ├── server.py       MCP entry point
│   ├── doctor.py       Installation diagnostic
│   ├── tools/          Agent-facing interfaces
│   └── core/           Session, API, inspection, solver and report implementations
├── tests/              Program tests; integration/ holds real COMSOL acceptance
├── scripts/            Installation, connection checks and release utilities
├── vendor/             Bundled Windows Python installer and dependency wheels
├── docs/               Architecture, GPT instructions and handoff conventions
└── .agents/            Employee Assistant execution Skills
```

See [architecture](docs/comsol-proposal.md) and
[maintenance and release instructions](RELEASE_CONTENTS.md).

## Local data

- Operation journals: `COMSOL_MCP_DATA_DIR/audit/`, defaulting to
  `.comsol-mcp-data/audit/` under the working directory.
- Report pages: the 16 most recently used reports in MCP process memory.
- Model saves and exports: absolute paths supplied by the task.
- Real acceptance reports: `integration_reports/` under the data directory,
  unless an explicit report directory is supplied.

Keep the data directory outside the checkout. Users select material suitable
for sharing with external GPT under company rules.

Derived from [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP)
under the MIT License. See `LICENSE` and [NOTICE.md](NOTICE.md).
