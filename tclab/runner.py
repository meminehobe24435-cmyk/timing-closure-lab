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
    "tools_available",
    "probe_tool",
    "tool_supports",
    "looks_like_missing_tool",
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


#: Phrases a shell prints when it cannot find the executable. These reach us as
#: ordinary *output* (the shell itself ran fine and returned non-zero), so a
#: naive "did the command produce text?" availability probe mistakes them for a
#: version banner.
_MISSING_MARKERS = (
    "is not recognized as an internal or external command",
    "is not recognized as an internal or external",
    "command not found",
    "No such file or directory",
    "cannot find the file",
)

#: The tools this lab drives, with the flag that makes each print its version.
TOOL_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("yosys", ("-V",)),
    ("nextpnr-ecp5", ("--version",)),
    ("iverilog", ("-V",)),
)


def looks_like_missing_tool(output: str) -> bool:
    """True when *output* is a shell complaining that the tool does not exist."""
    low = output.lower()
    return any(m.lower() in low for m in _MISSING_MARKERS)


def probe_tool(
    tool: str,
    args: Sequence[str] = (),
    chain: Toolchain | None = None,
    timeout: float = 60.0,
) -> tuple[bool, str]:
    """Return ``(available, version_line)`` for one tool.

    Availability is decided by what the tool *said*, not by its exit code:
    ``nextpnr-ecp5 --version`` legitimately prints its banner to stderr and
    exits 1, while a missing binary makes the shell print a "not recognized"
    message and also exits non-zero. Only the message distinguishes them.
    """
    try:
        r = run_tool(tool, list(args), check=False, timeout=timeout, chain=chain)
    except (ToolError, ToolUnavailable) as exc:
        return False, f"UNAVAILABLE ({exc})"

    text = (r.stdout + "\n" + r.stderr).strip()
    if looks_like_missing_tool(text):
        first = text.splitlines()[0].strip()
        return False, f"UNAVAILABLE (not found: {first[:80]})"
    if not text:
        return False, f"UNAVAILABLE (no output, exit {r.returncode})"
    return True, text.splitlines()[0].strip()


def tool_versions(chain: Toolchain | None = None) -> dict[str, str]:
    """Version string per tool, or an explicit ``UNAVAILABLE (...)`` marker."""
    return {tool: probe_tool(tool, args, chain=chain)[1] for tool, args in TOOL_PROBES}


def tools_available(chain: Toolchain | None = None) -> bool:
    """True only when every tool this lab needs actually runs.

    This is the gate the test suite uses to decide whether the EDA-backed
    integration tests can run. Getting it wrong in the permissive direction
    makes those tests *fail* on a machine without the toolchain instead of
    skipping, which is exactly the false signal this project must not emit.
    """
    return all(probe_tool(tool, args, chain=chain)[0] for tool, args in TOOL_PROBES)


_HELP_CACHE: dict[tuple[str, str], bool] = {}


def tool_supports(
    tool: str,
    flag: str,
    chain: Toolchain | None = None,
    timeout: float = 60.0,
) -> bool:
    """Does *tool* advertise *flag* in its ``--help`` output?

    Version drift is real between the OSS CAD Suite build this lab was developed
    against and the older packages a distro ships. Notably, the ``nextpnr-ecp5``
    in Ubuntu's archive is old enough to have **no ``--sdc`` option at all**,
    while nextpnr-0.10-82 has one. Probing beats assuming: without this the flow
    hard-fails on any machine whose nextpnr predates SDC support.
    """
    key = (tool, flag)
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    try:
        r = run_tool(tool, ["--help"], check=False, timeout=timeout, chain=chain)
        text = r.output
    except (ToolError, ToolUnavailable):
        text = ""
    supported = flag in text
    _HELP_CACHE[key] = supported
    return supported
