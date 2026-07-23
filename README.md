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

```bash
.venv/bin/comsol-mcp
```

For Codex, copy and edit `examples/codex.config.toml.example`. Every path in
that template is a placeholder and must be replaced with an absolute path on
the target machine.

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
