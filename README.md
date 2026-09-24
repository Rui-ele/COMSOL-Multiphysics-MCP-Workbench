# COMSOL MCP Workbench

A reusable local MCP server for safe COMSOL Multiphysics automation. It lets an
MCP client and COMSOL Desktop work with the same model through a multi-client
COMSOL Server.

[中文说明](README_CN.md)

## What this distribution adds

- Discover and attach models already held by another COMSOL client.
- Default external models to read-only `observe` access.
- Require an explicit switch to `write` before mutation or solving.
- Prevent MCP from saving or deleting externally owned models.
- Preserve a concise audit trail and create simulation handoff reports.
- Verify the handoff contract with an opt-in real two-client test.

## Structured parameter-task PoC

The first workflow for [proposal A](docs/comsol-proposal.md) is a single, expert-approved parameter change. The [task protocol](docs/task-protocol.md) defines the JSON task and its structured result; [AGENTS.md](AGENTS.md) and the [parameter-task skill](.agents/skills/comsol-parameter-task/SKILL.md) define the local operator workflow.

For an agent, use the restricted `src.task_server` entry point in [the task MCP config example](examples/codex.task.config.toml.example). Its tools connect to an already running COMSOL Server, attach by tag, read a parameter with `param_get`, and run the parameter-task flow. Broad model discovery/inspection, parameter listing, general `param_set`, mesh, and solve tools are absent. `model_attach` and `param_get` can still address other known tags and names; the effective read scope depends on deployment permissions and expert rules. The full `src.server` remains available for internal expert-operated workflows. External GPT does not connect directly to MCP; the expert selects any context to share.

The expert obtains the current model's exact COMSOL tag in Desktop or the internal full MCP and supplies that tag and selected parameter information to the agent. The agent attaches that tag, then calls `task_parameter_preview` while it is in `observe` mode. Show the complete task and live readback to the expert. The expert runs `comsol-task-approve <task_id> --data-dir <MCP data directory>` in a local interactive terminal, reviews the preview, and enters the requested confirmation phrase. The agent must not run that CLI. After the expert reports approval and `task_parameter_status(task_id)` confirms `approved`, explicitly switch the same model to `write`, call `task_parameter_execute(model_name, task)`, and restore an externally attached model to `observe`. The execute tool rechecks the initial expression and reads the changed expression back. The CLI data directory must match MCP's `COMSOL_MCP_DATA_DIR`; remote approval through the employee assistant is not yet implemented. Task status remains queryable after an MCP restart, but an older preview cannot be executed after a restart, reconnect, or new model attachment. Prepare and approve a new `task_id` instead.

The earlier two-client acceptance model was temporary and has been stopped; its tag is not a reusable target. This PoC does not build a mesh, solve, save a model, or schedule later compute jobs. Live COMSOL acceptance is to be run on a company machine with an available license.

The local CLI and SQLite ledger do not authenticate a separate human identity. In a local Codex setup, an agent with arbitrary shell access under the same system account or direct database write access could bypass approval. A company deployment needs a trusted service boundary for MCP, approval, and task data, with the agent's arbitrary shell and database access restricted. A trusted employee-assistant approval interface remains to be built.

## Requirements

- Python 3.10 or newer.
- A locally installed COMSOL Multiphysics version supported by MPh.
- A valid COMSOL license for live operations.

## Install

```bash
python3 scripts/bootstrap.py
.venv/bin/comsol-mcp-doctor
```

On Windows, use `.venv\Scripts\comsol-mcp-doctor.exe`.

The core installation does not include PDF indexing dependencies. To enable the
optional local documentation search:

```bash
python3 scripts/bootstrap.py --knowledge
```

## Run

Use the restricted task entry point:

```bash
.venv/bin/comsol-mcp-task
```

For Codex, copy and edit `examples/codex.task.config.toml.example`. Every path
in that template is a placeholder and must be replaced with an absolute path
on the target machine. The full `examples/codex.config.toml.example` exposes
general write, mesh, and solve tools and is for expert manual operation only.
The restricted entry point connects to an already running COMSOL Server through
`comsol_connect`.

## Validate

```bash
.venv/bin/pytest
.venv/bin/python -m build
```

The default test suite never starts COMSOL. The real two-client acceptance test
is opt-in and uses a temporary port and an unsaved in-memory model:

```bash
.venv/bin/pytest -m integration
```

## Local data boundary

Models, official manuals, vector databases, audit logs, reports, credentials,
and machine-specific MCP configuration are intentionally excluded from this
repository. See `RELEASE_CONTENTS.md` for the publishable boundary.

## Upstream

This project is derived from `wjc9011/COMSOL_Multiphysics_MCP` under the MIT
License. See `NOTICE.md`.
