"""tclab.runner -- locating and invoking the EDA tools.

Every external tool call in this project funnels through :func:`run_tool` so
that the same code path works locally (Windows + OSS CAD Suite, invoked via
``tools/oss.bat``) and in CI (Linux, invoked via ``tools/oss.sh``).

Two things this module deliberately handles:

**One wrapper, two platforms.** The wrapper script sets up PATH for the
toolchain, so no Python code ever needs to know where ``yosys`` actually lives.
Set ``TCLAB_WRAPPER`` to point at a different wrapper.

**Non-ASCII working directories.** This lab lives under a path containing CJK
characters. ``cmd.exe`` re-encodes the command line it receives, so an absolute
path with non-ASCII characters passed *through* ``cmd /c`` arrives mangled at
the child process -- yosys then reports a file-not-found for a path that
plainly exists. The fix is to never put such a path on the command line: every
path handed to a tool is first rewritten relative to the working directory, and
the child runs with that directory as its cwd. :func:`run_tool` does this
automatically and refuses to silently pass through a non-ASCII absolute path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ToolError",
    "ToolResult",
    "Toolchain",
    "find_wrapper",
    "find_tool",
    "run_tool",
    "tool_versions",
    "ToolUnavailable",
]


class ToolError(Exception):
    """An external tool exited non-zero."""

    def __init__(self, message: str, result: "ToolResult | None" = None):
        super().__init__(message)
        self.result = result


class ToolUnavailable(Exception):
    """A required tool could not be located on this machine."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Uniform record of one external command."""

    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    cwd: str = ""
    log_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return self.stdout + ("\n" + self.stderr if self.stderr else "")

    def matches(self, pattern: str, flags: int = 0) -> list[str]:
        import re

        return re.findall(pattern, self.output, flags)

    def as_dict(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "duration_s": self.duration_s,
            "cwd": self.cwd,
            "log_path": self.log_path,
        }


# ---------------------------------------------------------------------------
# Locating things
# ---------------------------------------------------------------------------

_REPO_ROOT: Path | None = None


def repo_root() -> Path:
    """The project root (the directory containing ``tclab`` and ``tools``)."""
    global _REPO_ROOT
    if _REPO_ROOT is None:
        _REPO_ROOT = Path(__file__).resolve().parent.parent
    return _REPO_ROOT


def find_wrapper(root: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate the EDA environment wrapper script.

    Order: ``$TCLAB_WRAPPER``, then ``tools/oss.bat`` (Windows) or
    ``tools/oss.sh`` (POSIX) under the project root.
    """
    env = os.environ.get("TCLAB_WRAPPER")
    if env:
        p = Path(env)
        if p.exists():
            return p
        raise ToolUnavailable(f"TCLAB_WRAPPER points at a missing file: {p}")

    base = Path(root) if root else repo_root()
    candidates = ["tools/oss.bat", "tools/oss.sh"] if os.name == "nt" else [
        "tools/oss.sh",
        "tools/oss.bat",
    ]
    for c in candidates:
        p = base / c
        if p.exists():
            return p
    return None


def find_tool(name: str) -> str | None:
    """Locate a bare executable on PATH (used for the CI/apt fallback path)."""
    return shutil.which(name)


@dataclass
class Toolchain:
    """The resolved command prefix for invoking EDA tools."""

    wrapper: Path | None = None
    use_cmd_shell: bool = os.name == "nt"
    prefix: list[str] = field(default_factory=list)

    @classmethod
    def resolve(cls, root: str | os.PathLike[str] | None = None) -> "Toolchain":
        w = find_wrapper(root)
        if w is None:
            return cls(wrapper=None, prefix=[])
        prefix: list[str] = []
        if os.name == "nt" and w.suffix.lower() in (".bat", ".cmd"):
            prefix = ["cmd", "/c"]
        elif w.suffix.lower() == ".sh" and os.name != "nt":
            prefix = ["bash"]
        return cls(wrapper=w, prefix=prefix)

    def argv_for(self, tool: str, args: Sequence[str], base: Path | None = None) -> list[str]:
        argv: list[str] = list(self.prefix)
        if self.wrapper is not None:
            # The wrapper lives inside the repo, and this repo's path contains
            # non-ASCII characters. cmd.exe re-encodes the command line it is
            # handed, so an absolute non-ASCII path reaches the child mangled.
            # Rewriting it relative to the working directory keeps argv pure
            # ASCII -- the same trick _relativise() applies to every argument.
            wrapper = str(self.wrapper)
            if base is not None:
                try:
                    rel = self.wrapper.resolve().relative_to(Path(base).resolve())
                    # The wrapper is the command cmd.exe has to *find*, so it
                    # needs the native separator; forward slashes make cmd parse
                    # "tools/oss.bat" as a switch.
                    wrapper = str(rel) if os.name == "nt" else rel.as_posix()
                except (ValueError, OSError):
                    pass
            argv.append(wrapper)
        argv.append(tool)
        argv.extend(str(a) for a in args)
        return argv

    def describe(self) -> str:
        """Human-readable wrapper location, relative to the repo when possible.

        Relative on purpose: the absolute path contains this machine's working
        directory, and this string ends up in console output and in the JSON
        report that is committed to the repository.
        """
        if self.wrapper is None:
            return "no wrapper (tools must be on PATH)"
        try:
            return self.wrapper.resolve().relative_to(repo_root()).as_posix()
        except (ValueError, OSError):
            return str(self.wrapper)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _relativise(arg: str, cwd: Path) -> str:
    """Rewrite an absolute path under *cwd* as a relative one.

    This is what keeps non-ASCII directory names off the ``cmd.exe`` command
    line (see the module docstring).
    """
    if not arg or arg.startswith("-") or "://" in arg:
        return arg
    try:
        p = Path(arg)
    except (OSError, ValueError):
        return arg
    if not p.is_absolute():
        return arg
    try:
        rel = p.resolve().relative_to(cwd.resolve())
    except (ValueError, OSError):
        return arg
    text = rel.as_posix()
    return text if text else "."


def _assert_ascii_argv(argv: Sequence[str]) -> None:
    bad = [a for a in argv if not str(a).isascii()]
    if bad:
        raise ToolUnavailable(
            "refusing to pass non-ASCII arguments through the tool wrapper "
            f"(this breaks cmd.exe on Windows): {bad!r}. "
            "Use paths relative to the working directory instead."
        )


def _terminate_tree(proc: "subprocess.Popen[str]") -> None:
    """Kill *proc* and every process it spawned.

    On Windows the command is routed through ``cmd.exe`` (to run the .bat
    wrapper), so the tool is a grandchild. ``Popen.kill`` only reaps the direct
    child, leaving the tool alive and holding the inherited pipes -- which makes
    ``communicate`` block forever. ``taskkill /T`` walks the tree instead.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_tool(
    tool: str,
    args: Sequence[str] = (),
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = 1800.0,
    check: bool = True,
    chain: Toolchain | None = None,
    log_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> ToolResult:
    """Invoke an EDA tool through the environment wrapper.

    :param tool: bare tool name, e.g. ``"yosys"``.
    :param args: its arguments.
    :param cwd: working directory; defaults to the project root.
    :param check: raise :class:`ToolError` on a non-zero exit.
    :param log_path: also write combined output here (relative to *cwd*).
    """
    import time

    base = Path(cwd).resolve() if cwd else repo_root()
    tc = chain or Toolchain.resolve(base)

    rel_args = [_relativise(str(a), base) for a in args]
    argv = tc.argv_for(tool, rel_args, base=base)
    _assert_ascii_argv(argv)

    child_env = dict(os.environ)
    # The lab is offline-by-design: a stale proxy environment variable makes
    # every tool call that touches the network hang instead of failing fast.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        child_env.pop(var, None)
    if env:
        child_env.update(env)

    log_abs: Path | None = None
    if log_path is not None:
        log_abs = (base / log_path) if not Path(log_path).is_absolute() else Path(log_path)
        log_abs.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(base),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ToolUnavailable(
            f"cannot execute {tool!r} via {tc.describe()}: {exc}"
        ) from exc

    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
        out, err = out or "", err or ""
    except subprocess.TimeoutExpired:
        # Killing the direct child is not enough: on Windows the child is
        # ``cmd.exe`` and the real tool is a grandchild that inherits the
        # stdout/stderr pipes. Terminating only cmd.exe leaves the tool running
        # and holding the pipes open, so communicate() never returns and the
        # timeout silently becomes an infinite hang. The whole tree has to go.
        _terminate_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        out, err = out or "", err or ""
        rc = -9
        err = (err + f"\nTIMEOUT after {timeout}s (process tree terminated)").strip()
    duration = time.time() - start

    result = ToolResult(
        argv=argv,
        returncode=rc,
        stdout=out,
        stderr=err,
        duration_s=duration,
        cwd=str(base),
        log_path=str(log_abs) if log_abs else None,
    )
    if log_abs is not None:
        with open(log_abs, "w", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(argv)}\n")
            fh.write(f"# cwd={base}\n")
            fh.write(f"# exit={rc} duration={duration:.2f}s\n")
            fh.write("=" * 70 + "\n")
            fh.write(out)
            if err:
                fh.write("\n--- stderr ---\n")
                fh.write(err)

    if check and rc != 0:
        tail = "\n".join(result.output.strip().splitlines()[-15:])
        raise ToolError(
            f"{tool} exited {rc} after {duration:.1f}s\n"
            f"command: {' '.join(argv)}\nlast output:\n{tail}",
            result,
        )
    return result


def tool_versions(chain: Toolchain | None = None) -> dict[str, str]:
    """Best-effort version strings for the tools this lab uses."""
    out: dict[str, str] = {}
    probes = [
        ("yosys", ["-V"]),
        ("nextpnr-ecp5", ["--version"]),
        ("iverilog", ["-V"]),
    ]
    for tool, args in probes:
        try:
            r = run_tool(tool, args, check=False, timeout=60, chain=chain)
            text = (r.stdout + " " + r.stderr).strip().splitlines()
            out[tool] = text[0].strip() if text else f"exit {r.returncode}"
        except (ToolError, ToolUnavailable) as exc:
            out[tool] = f"UNAVAILABLE ({exc})"
    return out
