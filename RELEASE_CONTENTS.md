# Release contents

## Included

- `src/`: MCP server, tools, resources, access control, reporting, and embedded
  text guidance.
- `AGENTS.md`, `.agents/skills/`, and `docs/`: project task rules, the
  parameter workflow, and its versioned protocol.
- `tests/`: unit and contract tests; the real COMSOL test is opt-in.
- `scripts/`: bootstrap, knowledge-base build, release audit, and real
  two-client acceptance.
- `examples/`: placeholder-only client and environment configuration.
- Project metadata, English and Chinese documentation, MIT license, and
  upstream notice.

## Explicitly excluded

- Git history from the development checkout.
- COMSOL `.mph` models and autosave files.
- COMSOL software, license material, and official PDF manuals.
- Vector databases, downloaded embedding models, and other caches.
- Audit logs, simulation reports, integration reports, and temporary files.
- Usernames, passwords, tokens, local credentials, and machine-specific
  configuration.
- The development-only duplicate `src/tools/session 2.py`.

## Release audit

Run:

```bash
python scripts/release_audit.py
```

The audit fails if a forbidden artifact, oversized file, development checkout
path, or current user home path is present in the release tree.
