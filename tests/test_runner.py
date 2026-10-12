"""Tests for tclab.runner -- tool discovery, argument handling and error paths.

These do not need EDA tools installed: they exercise the path-relativisation and
guard logic that keeps the flow working from a directory whose name contains
non-ASCII characters, plus the failure modes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tclab import runner as R

class _FakePopen:
    """Minimal stand-in for subprocess.Popen as run_tool uses it."""

    def __init__(self, argv=None, **kwargs):
        self.argv = argv or []
        self.kwargs = kwargs
        self.returncode = 0
        self.pid = 4242
        self._out = ""
        self._err = ""
        self.calls = 1
        _LAST_POPEN.append(self)

    def communicate(self, timeout=None):
        return self._out, self._err

    def poll(self):
        return self.returncode

    def kill(self):
        pass

    def terminate(self):
        pass


_LAST_POPEN: list = []


def _fake_popen(returncode=0, stdout="", stderr=""):
    """Build a Popen replacement that returns fixed output.

    The most recent instance is kept in ``_LAST_POPEN`` so a test can inspect
    exactly what argv and environment ``run_tool`` handed to the process.
    """

    def factory(*args, **kwargs):
        _LAST_POPEN.clear()
        p = _FakePopen(*args, **kwargs)
        p.returncode = returncode
        p._out = stdout
        p._err = stderr
        return p

    return factory



# ---------------------------------------------------------------------------
# Toolchain discovery
# ---------------------------------------------------------------------------


def test_repo_root_is_the_project_directory():
    root = R.repo_root()
    assert (root / "tclab").is_dir()
    assert (root / "rtl").is_dir()


def test_find_wrapper_locates_a_shipped_wrapper():
    w = R.find_wrapper()
    assert w is not None
    assert w.name in ("oss.bat", "oss.sh")
    assert w.exists()


def test_find_wrapper_honours_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "custom.bat"
    fake.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("TCLAB_WRAPPER", str(fake))
    assert R.find_wrapper() == fake


def test_find_wrapper_rejects_missing_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TCLAB_WRAPPER", str(tmp_path / "nope.bat"))
    with pytest.raises(R.ToolUnavailable):
        R.find_wrapper()


def test_find_wrapper_returns_none_for_empty_tree(tmp_path):
    assert R.find_wrapper(tmp_path) is None


def test_toolchain_resolve_has_a_wrapper_here():
    tc = R.Toolchain.resolve()
    assert tc.wrapper is not None
    assert tc.describe() != "no wrapper (tools must be on PATH)"


def test_toolchain_argv_shape_when_wrapper_present():
    tc = R.Toolchain.resolve()
    argv = tc.argv_for("yosys", ["-V"], base=R.repo_root())
    assert "yosys" in argv
    assert argv[-1] == "-V"


def test_toolchain_without_wrapper_invokes_tool_directly():
    tc = R.Toolchain(wrapper=None, prefix=[])
    argv = tc.argv_for("yosys", ["-V"])
    assert argv == ["yosys", "-V"]
    assert tc.describe() == "no wrapper (tools must be on PATH)"


# ---------------------------------------------------------------------------
# Path relativisation: the non-ASCII workaround
# ---------------------------------------------------------------------------


def test_relativise_absolute_path_inside_cwd():
    root = R.repo_root()
    got = R._relativise(str(root / "reports" / "x.json"), root)
    assert got == "reports/x.json"


def test_relativise_leaves_relative_paths_alone():
    root = R.repo_root()
    assert R._relativise("rtl/fir_core.v", root) == "rtl/fir_core.v"


def test_relativise_leaves_options_alone():
    root = R.repo_root()
    assert R._relativise("--json", root) == "--json"
    assert R._relativise("-V", root) == "-V"


def test_relativise_leaves_outside_paths_alone():
    root = R.repo_root()
    outside = "C:/Windows/System32/drivers/etc/hosts" if os.name == "nt" else "/etc/hosts"
    assert R._relativise(outside, root) == outside


def test_ascii_guard_rejects_non_ascii_arguments():
    """Regression: this repo's own path contains CJK characters.

    Passing such a path through ``cmd.exe`` mangles it, so the runner must refuse
    rather than silently hand the tool a path that does not exist.
    """
    with pytest.raises(R.ToolUnavailable) as ei:
        R._assert_ascii_argv(["yosys", "D:\\work\\工程\\report.json"])
    assert "non-ASCII" in str(ei.value)


def test_ascii_guard_accepts_pure_ascii():
    R._assert_ascii_argv(["yosys", "-V", "reports/x.json"])


def test_run_tool_relativises_arguments_before_executing(tmp_path, monkeypatch):
    """An absolute in-tree path must reach the tool as a relative one."""
    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen(0, "ok", ""))
    root = R.repo_root()
    R.run_tool(
        "yosys",
        ["-p", "script", "--json", str(root / "reports" / "out.json")],
        cwd=root,
        chain=R.Toolchain(wrapper=None, prefix=[]),
    )
    argv = [str(a) for a in _LAST_POPEN[-1].argv]
    assert "reports/out.json" in argv or "reports\\out.json" in argv
    assert not any("工程" in a for a in argv)


def test_run_tool_strips_proxy_environment(tmp_path, monkeypatch):
    """A dead proxy in the environment makes tools hang; the runner clears it."""
    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen(0, "", ""))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    R.run_tool("yosys", ["-V"], cwd=R.repo_root(), chain=R.Toolchain(wrapper=None, prefix=[]))
    env = _LAST_POPEN[-1].kwargs.get("env", {})
    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_run_tool_raises_toolunavailable_for_missing_binary():
    with pytest.raises(R.ToolUnavailable):
        R.run_tool(
            "definitely-not-a-real-tool-xyz",
            ["--version"],
            cwd=R.repo_root(),
            chain=R.Toolchain(wrapper=None, prefix=[]),
            timeout=20,
        )


def test_run_tool_check_false_returns_result_instead_of_raising(monkeypatch):
    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen(7, "some output", "some error"))
    r = R.run_tool(
        "yosys", ["-V"], cwd=R.repo_root(), chain=R.Toolchain(wrapper=None, prefix=[]), check=False
    )
    assert r.returncode == 7
    assert not r.ok
    assert "some output" in r.output
    assert "some error" in r.output


def test_run_tool_check_true_raises_toolerror_with_output(monkeypatch):
    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen(3, "line one\nline two", ""))
    with pytest.raises(R.ToolError) as ei:
        R.run_tool("yosys", ["-V"], cwd=R.repo_root(), chain=R.Toolchain(wrapper=None, prefix=[]))
    assert ei.value.result is not None
    assert ei.value.result.returncode == 3
    assert "line two" in str(ei.value)


def test_run_tool_writes_a_log_file(monkeypatch, tmp_path):
    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen(0, "hello from the tool", ""))
    log = tmp_path / "run.log"
    r = R.run_tool(
        "yosys",
        ["-V"],
        cwd=R.repo_root(),
        chain=R.Toolchain(wrapper=None, prefix=[]),
        log_path=str(log),
    )
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "hello from the tool" in text
    assert "exit=0" in text
    assert r.log_path == str(log)


def test_tool_result_matches_regex(monkeypatch):
    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen(0, "Max frequency for clock 'clk': 51.79 MHz", ""))
    r = R.run_tool("yosys", ["-V"], cwd=R.repo_root(), chain=R.Toolchain(wrapper=None, prefix=[]))
    assert r.matches(r"([0-9.]+) MHz") == ["51.79"]


def test_tool_result_as_dict_is_json_serialisable(monkeypatch):
    import json

    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen(0, "", ""))
    r = R.run_tool("yosys", ["-V"], cwd=R.repo_root(), chain=R.Toolchain(wrapper=None, prefix=[]))
    json.dumps(r.as_dict())


def test_tool_versions_reports_unavailable_rather_than_raising(monkeypatch):
    monkeypatch.setenv("TCLAB_WRAPPER", str(R.repo_root() / "does" / "not" / "exist.bat"))
    versions = R.tool_versions()
    assert set(versions) == {"yosys", "nextpnr-ecp5", "iverilog"}
    assert all(isinstance(v, str) for v in versions.values())
