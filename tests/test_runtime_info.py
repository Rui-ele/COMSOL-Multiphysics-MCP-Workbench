"""Report metadata must not start child processes during MCP tool calls."""

import subprocess

import pytest

from src.core.runtime_info import _source_revision
from src.core.tool_runtime import AuditRecorder, AuditedFastMCP, _result_payload
from src.tools.session import register_session_tools


REVISION = "1234567890abcdef1234567890abcdef12345678"


@pytest.mark.parametrize("layout", ["loose", "packed", "detached", "worktree"])
def test_revision_from_git_files(tmp_path, layout):
    git_dir = tmp_path / ".git"
    common = git_dir
    if layout == "worktree":
        common = tmp_path / "metadata"
        actual = common / "worktrees" / "test"
        actual.mkdir(parents=True)
        git_dir.write_text("gitdir: metadata/worktrees/test\n", encoding="utf-8")
        git_dir = actual
        (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    else:
        git_dir.mkdir()
    (git_dir / "HEAD").write_text(
        REVISION if layout == "detached" else "ref: refs/heads/main\n",
        encoding="utf-8",
    )
    if layout in {"packed", "worktree"}:
        (common / "packed-refs").write_text(
            f"# pack-refs with: peeled\n{REVISION} refs/heads/main\n", encoding="utf-8")
    elif layout == "loose":
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "refs" / "heads" / "main").write_text(REVISION, encoding="utf-8")
    assert _source_revision(tmp_path) == REVISION


@pytest.mark.asyncio
async def test_status_and_reports_do_not_launch_git(monkeypatch, tmp_path):
    launched = []

    def unavailable_process(*args, **kwargs):
        launched.append(args)
        raise OSError("Git process launch is unavailable")

    monkeypatch.setattr(subprocess, "Popen", unavailable_process)
    mcp = AuditedFastMCP("status-without-git", recorder=AuditRecorder(tmp_path))
    register_session_tools(mcp)
    result = _result_payload(await mcp.call_tool("comsol_status", {}))
    assert result["success"] is True
    assert result["connected"] is False
    assert result["runtime"]["source_dirty"] is None
    assert result["runtime"]["source_dirty_status"] == "not_checked"
    assert "report_markdown" in result
    assert launched == []
