"""tclab.sdc -- a parser for the TCL/SDC constraint subset used in this lab.

SDC is TCL, so a real SDC file is a real TCL script. Rather than pretend the
constraint file is a flat list of lines, this module implements the part of the
TCL language that SDC files actually use:

  * comments ``# ...`` to end of line
  * line continuation with a trailing backslash
  * command separation by newline or ``;``
  * brace grouping ``{ ... }`` -- literal, nested, no substitution
  * command substitution ``[ ... ]`` -- nested scripts, e.g. ``[get_ports clk]``
  * double quotes ``" ... "`` -- grouping with substitution
  * variables: ``set name value`` then ``$name`` or ``${name}``
  * backslash escapes in bare words (``\\ `` and ``\\$``)

Everything it does not implement raises :class:`SdcSyntaxError` or
:class:`SdcUnknownCommandError` carrying the source line, so a malformed
constraint file fails loudly instead of being silently misread.

The parse result is a plain, inspectable object graph (:class:`SdcDesign`),
which is what the rest of the lab reasons about. This is deliberately *not* a
TCL interpreter -- it is an SDC-aware parser with just enough TCL front end to
read real constraint files written by a human or exported by a tool.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "SdcError",
    "SdcSyntaxError",
    "SdcUnknownCommandError",
    "SdcEvaluationError",
    "Selector",
    "Word",
    "Command",
    "Clock",
    "GeneratedClock",
    "IoDelay",
    "MaxDelay",
    "FalsePath",
    "MulticyclePath",
    "ClockGroup",
    "SdcDesign",
    "parse",
    "parse_file",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SdcError(Exception):
    """Base class for every failure raised by this module."""

    def __init__(self, message: str, line: int | None = None, filename: str | None = None):
        self.message = message
        self.line = line
        self.filename = filename
        where = ""
        if filename and line:
            where = f"{filename}:{line}: "
        elif line:
            where = f"line {line}: "
        elif filename:
            where = f"{filename}: "
        super().__init__(f"{where}{message}")


class SdcSyntaxError(SdcError):
    """Malformed TCL/SDC: unbalanced brace, bracket or quote, bad word."""


class SdcUnknownCommandError(SdcError):
    """A command that this parser does not know about (strict mode only)."""


class SdcEvaluationError(SdcError):
    """Well-formed but not evaluable: undefined variable, bad selector..."""


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selector:
    """The result of a TCL command substitution such as ``[get_ports clk]``.

    SDC selectors address design objects by kind (ports, pins, nets, clocks)
    and by one or more name patterns. We keep the patterns rather than
    resolving them, because resolving them is the job of a design database and
    this lab's database is the constraint file itself.

    ``patterns == ("*",)`` is used for the wildcard form ``[get_ports *]``.
    """

    kind: str
    patterns: tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.kind}({', '.join(self.patterns)})"

    @property
    def is_wildcard(self) -> bool:
        return any("*" in p or "?" in p for p in self.patterns)


@dataclass
class Word:
    """A single TCL word: a concatenation of literal text and substitutions."""

    parts: list[Any] = field(default_factory=list)
    braced: bool = False
    line: int | None = None

    def append(self, part: Any) -> None:
        self.parts.append(part)

    @property
    def selectors(self) -> list[Selector]:
        return [p for p in self.parts if isinstance(p, Selector)]

    def as_text(self) -> str:
        """Flatten to text, rendering any selector in its ``kind(a, b)`` form."""
        out: list[str] = []
        for p in self.parts:
            if isinstance(p, Selector):
                out.append(str(p))
            else:
                out.append(str(p))
        return "".join(out)

    def as_scalar(self) -> Any:
        """Return the single value of this word.

        A word that is exactly one selector returns that :class:`Selector`;
        anything else returns its text form.
        """
        if len(self.parts) == 1:
            return self.parts[0]
        return self.as_text()

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.as_text()


@dataclass
class Command:
    """A parsed SDC/TCL command: a name, its argument words, and its line."""

    name: str
    args: list[Word]
    line: int


# ---------------------------------------------------------------------------
# Constraint object model
# ---------------------------------------------------------------------------


@dataclass
class Clock:
    """``create_clock``."""

    name: str
    period_ns: float
    target: Any = None
    waveform: tuple[float, ...] | None = None
    line: int | None = None

    @property
    def frequency_mhz(self) -> float:
        return 1000.0 / self.period_ns


@dataclass
class GeneratedClock:
    """``create_generated_clock``."""

    name: str
    source: Any = None
    target: Any = None
    divide_by: int | None = None
    multiply_by: int | None = None
    line: int | None = None

    def period_ns(self, primary: Sequence[Clock] = ()) -> float | None:
        """Derive the generated period from the named/source primary clock."""
        base = None
        for c in primary:
            if self.source is not None and _selector_mentions(self.source, c.name):
                base = c
                break
            if self.name and c.name == self.name:
                base = c
                break
        if base is None:
            base = next(iter(primary), None)
        if base is None:
            return None
        if self.divide_by:
            return base.period_ns * float(self.divide_by)
        if self.multiply_by:
            return base.period_ns / float(self.multiply_by)
        return base.period_ns


@dataclass
class IoDelay:
    """``set_input_delay`` / ``set_output_delay``."""

    kind: str  # "input" | "output"
    target: Any = None
    clock: Any = None
    max_ns: float | None = None
    min_ns: float | None = None
    line: int | None = None


@dataclass
class MaxDelay:
    """``set_max_delay``."""

    value_ns: float
    frm: Any = None
    to: Any = None
    through: Any = None
    line: int | None = None


@dataclass
class FalsePath:
    """``set_false_path``."""

    frm: Any = None
    to: Any = None
    through: Any = None
    line: int | None = None


@dataclass
class MulticyclePath:
    """``set_multicycle_path``."""

    frm: Any = None
    to: Any = None
    setup: int | None = None
    hold: int | None = None
    line: int | None = None


@dataclass
class ClockGroup:
    """``set_clock_groups``."""

    mode: str  # "asynchronous" | "exclusive" | "physically_exclusive" | "logically_exclusive"
    groups: list[list[str]] = field(default_factory=list)
    line: int | None = None


@dataclass
class SdcDesign:
    """Everything parsed out of one SDC file."""

    clocks: list[Clock] = field(default_factory=list)
    generated_clocks: list[GeneratedClock] = field(default_factory=list)
    input_delays: list[IoDelay] = field(default_factory=list)
    output_delays: list[IoDelay] = field(default_factory=list)
    max_delays: list[MaxDelay] = field(default_factory=list)
    false_paths: list[FalsePath] = field(default_factory=list)
    multicycle_paths: list[MulticyclePath] = field(default_factory=list)
    clock_groups: list[ClockGroup] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    commands: list[Command] = field(default_factory=list)
    filename: str | None = None

    # -- convenience --------------------------------------------------------

    def clock_by_name(self, name: str) -> Clock | None:
        for c in self.clocks:
            if c.name == name:
                return c
        return None

    @property
    def primary_period_ns(self) -> float | None:
        """Period of the first primary clock, which is the block's target."""
        if not self.clocks:
            return None
        return min(c.period_ns for c in self.clocks)

    @property
    def all_clock_names(self) -> list[str]:
        names = [c.name for c in self.clocks]
        names += [g.name for g in self.generated_clocks if g.name not in names]
        return names

    def command_names(self) -> list[str]:
        return [c.name for c in self.commands]

    def summary(self) -> dict[str, int]:
        """Counts per constraint family -- handy for the CLI and for tests."""
        return {
            "create_clock": len(self.clocks),
            "create_generated_clock": len(self.generated_clocks),
            "set_input_delay": len(self.input_delays),
            "set_output_delay": len(self.output_delays),
            "set_max_delay": len(self.max_delays),
            "set_false_path": len(self.false_paths),
            "set_multicycle_path": len(self.multicycle_paths),
            "set_clock_groups": len(self.clock_groups),
        }


def _selector_mentions(value: Any, name: str) -> bool:
    if isinstance(value, Selector):
        return name in value.patterns
    return isinstance(value, str) and value == name


# ---------------------------------------------------------------------------
# Lexer / parser
# ---------------------------------------------------------------------------

# Sets, not strings. Membership tests against a *string* are substring tests,
# and `"" in " \t\r"` is True -- so `peek()` returning "" at end of input made a
# whitespace-skip loop spin forever. Using a set makes the empty-string case
# behave the way the code reads.
_WS = frozenset(" \t\r")
_BARE_STOP = frozenset(" \t\r\n;]")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "$": "$", "[": "[", "]": "]"}


class _Parser:
    """Recursive-descent parser for the TCL subset described in the module docs."""

    def __init__(self, text: str, filename: str | None = None):
        self.text = text
        self.n = len(text)
        self.i = 0
        self.line = 1
        self.filename = filename
        self.variables: dict[str, Any] = {}

    # -- helpers ------------------------------------------------------------

    def fail(self, msg: str, line: int | None = None) -> None:
        raise SdcSyntaxError(msg, line if line is not None else self.line, self.filename)

    def fail_eval(self, msg: str, line: int | None = None) -> None:
        raise SdcEvaluationError(msg, line if line is not None else self.line, self.filename)

    def at_end(self) -> bool:
        return self.i >= self.n

    def peek(self) -> str:
        return self.text[self.i] if self.i < self.n else ""

    def advance(self, count: int = 1) -> None:
        for _ in range(count):
            if self.i < self.n:
                if self.text[self.i] == "\n":
                    self.line += 1
                self.i += 1

    def skip_separators_and_space(self) -> bool:
        """Skip whitespace, comments, line continuations and command separators.

        Returns True if at least one command separator (newline or ``;``) was
        consumed, which the caller uses to know a command has ended.
        """
        saw_sep = False
        while not self.at_end():
            ch = self.peek()
            if ch in _WS:
                self.advance()
            elif ch == "\n":
                saw_sep = True
                self.advance()
            elif ch == ";":
                saw_sep = True
                self.advance()
            elif ch == "#":
                while not self.at_end() and self.peek() != "\n":
                    self.advance()
            elif ch == "\\" and self.i + 1 < self.n and self.text[self.i + 1] == "\n":
                # line continuation: joins the next physical line
                self.advance(2)
            elif ch == "\\" and self.i + 2 < self.n and self.text[self.i + 1 : self.i + 3] == "\r\n":
                self.advance(3)
            else:
                break
        return saw_sep

    # -- script / command ---------------------------------------------------

    def parse_script(self) -> list[Command]:
        commands: list[Command] = []
        while True:
            self.skip_separators_and_space()
            if self.at_end():
                break
            cmd = self.parse_command()
            # `set` has to take effect *while* parsing, not afterwards: a later
            # word may reference $var, and word substitution happens during
            # parsing. Deferring all evaluation to the end (which is what this
            # used to do) makes every $var after a `set` look undefined.
            if cmd.name == "set":
                self.eval_one(cmd)
            commands.append(cmd)
        return commands

    def parse_command(self) -> Command:
        line = self.line
        words: list[Word] = []
        while True:
            # Inside a command, only spaces/tabs and continuations separate
            # words; newline or ';' terminates it.
            # The at_end() guard is load-bearing: without it, peek() returns ""
            # and a loop that skips characters never terminates at end of input.
            while not self.at_end():
                ch = self.peek()
                if ch in _WS:
                    self.advance()
                elif ch == "\\" and self.i + 1 < self.n and self.text[self.i + 1] in "\n\r":
                    self.advance(2 if self.text[self.i + 1] == "\n" else 3)
                else:
                    break
            if self.at_end() or self.peek() in "\n;]":
                break
            words.append(self.parse_word())
        if not words:
            self.fail("empty command", line)
        name = words[0].as_text()
        return Command(name=name, args=words[1:], line=line)

    # -- words --------------------------------------------------------------

    def parse_word(self) -> Word:
        line = self.line
        ch = self.peek()
        if ch == "{":
            return self.parse_braced(line)
        if ch == "[":
            return self.parse_bracket_group(line)
        if ch == '"':
            return self.parse_quoted(line)

        word = Word(line=line)
        buf: list[str] = []

        def flush() -> None:
            if buf:
                word.append("".join(buf))
                buf.clear()

        while not self.at_end():
            c = self.peek()
            if c in _BARE_STOP:
                break
            if c == "\\":
                if self.i + 1 >= self.n:
                    self.fail("trailing backslash at end of input")
                nxt = self.text[self.i + 1]
                if nxt == "\n":
                    self.advance(2)
                    continue
                buf.append(_ESCAPES.get(nxt, nxt))
                self.advance(2)
                continue
            if c == "$":
                flush()
                word.append(self.parse_variable())
                continue
            if c == "[":
                flush()
                sel = self.parse_command_substitution()
                word.append(sel)
                continue
            if c == "{":
                # A brace appearing mid-word is legal TCL only in limited
                # forms; treat it as a syntax error rather than guessing.
                self.fail("unexpected '{' inside a word (brace grouping must start the word)")
            if c == "}":
                self.fail("unmatched '}'")
            if c == '"':
                self.fail("unexpected '\"' inside a word (quoting must start the word)")
            buf.append(c)
            self.advance()
        flush()
        if not word.parts:
            self.fail("empty word", line)
        return word

    def parse_braced(self, line: int) -> Word:
        """``{ ... }`` -- literal content, nesting counted, no substitution."""
        assert self.peek() == "{"
        self.advance()
        depth = 1
        chars: list[str] = []
        while not self.at_end():
            c = self.peek()
            if c == "\\" and self.i + 1 < self.n:
                chars.append(self.text[self.i : self.i + 2])
                self.advance(2)
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    self.advance()
                    w = Word(braced=True, line=line)
                    w.append("".join(chars).strip())
                    return w
            chars.append(c)
            self.advance()
        self.fail("unbalanced '{': reached end of input before matching '}'", line)
        raise AssertionError("unreachable")

    def parse_quoted(self, line: int) -> Word:
        """``" ... "`` -- grouping with substitution enabled."""
        assert self.peek() == '"'
        self.advance()
        word = Word(line=line)
        buf: list[str] = []

        def flush() -> None:
            if buf:
                word.append("".join(buf))
                buf.clear()

        while not self.at_end():
            c = self.peek()
            if c == '"':
                self.advance()
                flush()
                return word
            if c == "\\":
                if self.i + 1 >= self.n:
                    self.fail("trailing backslash inside quotes")
                nxt = self.text[self.i + 1]
                if nxt == "\n":
                    self.advance(2)
                    continue
                buf.append(_ESCAPES.get(nxt, nxt))
                self.advance(2)
                continue
            if c == "$":
                flush()
                word.append(self.parse_variable())
                continue
            if c == "[":
                flush()
                word.append(self.parse_command_substitution())
                continue
            buf.append(c)
            self.advance()
        self.fail("unbalanced '\"': reached end of input before matching quote", line)
        raise AssertionError("unreachable")

    def parse_bracket_group(self, line: int) -> Word:
        sel = self.parse_command_substitution()
        w = Word(line=line)
        w.append(sel)
        return w

    def parse_variable(self) -> str:
        """``$name`` or ``${name}``."""
        assert self.peek() == "$"
        start_line = self.line
        self.advance()
        if self.peek() == "{":
            self.advance()
            chars: list[str] = []
            while not self.at_end() and self.peek() != "}":
                chars.append(self.peek())
                self.advance()
            if self.at_end():
                self.fail("unbalanced '${'", start_line)
            self.advance()
            name = "".join(chars)
        else:
            chars = []
            while not self.at_end() and (self.peek().isalnum() or self.peek() in "_:"):
                chars.append(self.peek())
                self.advance()
            name = "".join(chars)
        if not name:
            self.fail("'$' not followed by a variable name", start_line)
        if name not in self.variables:
            self.fail_eval(f"undefined variable ${name}", start_line)
        value = self.variables[name]
        return value if isinstance(value, str) else str(value)

    def parse_command_substitution(self) -> Any:
        """``[ ... ]`` -- a nested script, evaluated to a single value."""
        assert self.peek() == "["
        start_line = self.line
        self.advance()
        inner: list[Command] = []
        while True:
            self.skip_separators_and_space()
            if self.at_end():
                self.fail("unbalanced '[': reached end of input before matching ']'", start_line)
            if self.peek() == "]":
                self.advance()
                break
            inner.append(self.parse_command())
        if not inner:
            self.fail("empty command substitution '[]'", start_line)
        return self.evaluate(inner)

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, commands: list[Command]) -> Any:
        """Evaluate parsed commands for their value (used inside ``[ ]``)."""
        result: Any = ""
        for cmd in commands:
            result = self.eval_one(cmd)
        return result

    def eval_one(self, cmd: Command) -> Any:
        name = cmd.name
        args = [a.as_scalar() for a in cmd.args]
        text_args = [a.as_text() for a in cmd.args]

        if name in ("get_ports", "get_pins", "get_nets", "get_clocks", "get_cells"):
            kind = name[4:]
            patterns: list[str] = []
            for a in args:
                if isinstance(a, Selector):
                    patterns.extend(a.patterns)
                else:
                    patterns.extend(str(a).split())
            return Selector(kind=kind, patterns=tuple(patterns))

        if name == "set":
            if not text_args:
                self.fail_eval("set requires a variable name", cmd.line)
            varname = text_args[0]
            value: Any = ""
            if len(text_args) > 1:
                value = args[1] if len(args) > 1 else text_args[1]
            self.variables[varname] = value
            return value

        if name == "expr":
            # TCL's `expr` substitutes variables in its operands even when the
            # argument arrived inside braces, because braces suppress
            # substitution in the *word* but expr re-reads its own argument.
            return _eval_expr(_subst_vars(" ".join(text_args), self, cmd.line), self, cmd.line)

        if name == "list":
            return " ".join(text_args)

        if name == "concat":
            return " ".join(text_args)

        if name in ("llength",):
            return str(len(text_args))

        if name in ("lindex",):
            if len(text_args) < 2:
                self.fail_eval("lindex requires a list and an index", cmd.line)
            items = text_args[0].split()
            try:
                return items[int(text_args[1])]
            except (ValueError, IndexError):
                self.fail_eval(f"lindex index out of range: {text_args[1]!r}", cmd.line)

        self.fail_eval(f"unsupported command [{name} ...] in command substitution", cmd.line)
        raise AssertionError("unreachable")


_NUM_RE = re.compile(r"\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _subst_vars(text: str, parser: "_Parser", line: int) -> str:
    """Replace ``$name`` / ``${name}`` in *text* using the parser's variables."""

    def repl(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        if name not in parser.variables:
            parser.fail_eval(f"undefined variable ${name}", line)
        return str(parser.variables[name])

    return _VAR_RE.sub(repl, text)


def _eval_expr(expr: str, parser: _Parser, line: int) -> str:
    """A deliberately tiny arithmetic evaluator for ``[expr ...]``.

    Supports ``+ - * /`` with parentheses and numeric literals only. Anything
    else is rejected -- this is not a general expression engine.
    """
    tokens = re.findall(r"[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?|[+\-*/()]", expr)
    if not tokens or "".join(tokens) != re.sub(r"\s+", "", expr):
        parser.fail_eval(f"unsupported expression in [expr ...]: {expr!r}", line)
    pos = 0

    def peek() -> str:
        return tokens[pos] if pos < len(tokens) else ""

    def primary() -> float:
        nonlocal pos
        t = peek()
        if t == "(":
            pos += 1
            v = add()
            if peek() != ")":
                parser.fail_eval(f"unbalanced parentheses in [expr ...]: {expr!r}", line)
            pos += 1
            return v
        if t in ("-", "+"):
            pos += 1
            v = primary()
            return -v if t == "-" else v
        if not t:
            parser.fail_eval(f"incomplete expression in [expr ...]: {expr!r}", line)
        pos += 1
        try:
            return float(t)
        except ValueError:
            parser.fail_eval(f"bad number {t!r} in [expr ...]", line)
        raise AssertionError("unreachable")

    def mul() -> float:
        nonlocal pos
        v = primary()
        while peek() in ("*", "/"):
            op = peek()
            pos += 1
            r = primary()
            if op == "/":
                if r == 0:
                    parser.fail_eval("division by zero in [expr ...]", line)
                v /= r
            else:
                v *= r
        return v

    def add() -> float:
        nonlocal pos
        v = mul()
        while peek() in ("+", "-"):
            op = peek()
            pos += 1
            r = mul()
            v = v + r if op == "+" else v - r
        return v

    out = add()
    if pos != len(tokens):
        parser.fail_eval(f"trailing tokens in [expr ...]: {expr!r}", line)
    return _fmt_number(out)


def _fmt_number(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return repr(v)


# ---------------------------------------------------------------------------
# Command -> object translation
# ---------------------------------------------------------------------------

#: Commands this parser understands and turns into typed constraint objects.
KNOWN_COMMANDS = frozenset(
    {
        "create_clock",
        "create_generated_clock",
        "set_input_delay",
        "set_output_delay",
        "set_max_delay",
        "set_false_path",
        "set_multicycle_path",
        "set_clock_groups",
        "set",
    }
)

#: Real SDC files carry many other commands. These are accepted and retained
#: verbatim (they are not modelled) so that a realistic constraint file parses.
PASSTHROUGH_COMMANDS = frozenset(
    {
        "set_clock_uncertainty",
        "set_clock_latency",
        "set_clock_transition",
        "set_propagated_clock",
        "set_input_transition",
        "set_load",
        "set_driving_cell",
        "set_fanout_load",
        "set_max_fanout",
        "set_max_transition",
        "set_max_capacitance",
        "set_disable_timing",
        "set_case_analysis",
        "set_units",
        "set_operating_conditions",
        "set_wire_load_model",
        "current_design",
        "link_design",
        "read_verilog",
        "read_sdc",
        "puts",
    }
)


def _as_float(value: Any, what: str, line: int, filename: str | None) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    m = _NUM_RE.fullmatch(text)
    if not m:
        raise SdcEvaluationError(f"{what} must be numeric, got {text!r}", line, filename)
    return float(m.group(1))


def _as_int(value: Any, what: str, line: int, filename: str | None) -> int:
    return int(_as_float(value, what, line, filename))


def _take_options(
    cmd: Command,
    value_opts: Iterable[str],
    flag_opts: Iterable[str],
    filename: str | None,
) -> tuple[dict[str, Any], list[Word]]:
    """Split a command's words into ``-opt value`` pairs, flags, and positionals."""
    values = set(value_opts)
    flags = set(flag_opts)
    opts: dict[str, Any] = {}
    positional: list[Word] = []
    i = 0
    args = cmd.args
    while i < len(args):
        w = args[i]
        text = w.as_text()
        if text.startswith("-") and len(text) > 1 and not w.braced:
            key = text[1:]
            if key in flags:
                opts[key] = True
                i += 1
                continue
            if key in values:
                if i + 1 >= len(args):
                    raise SdcSyntaxError(
                        f"option -{key} to {cmd.name} requires a value", cmd.line, filename
                    )
                opts[key] = args[i + 1].as_scalar()
                i += 2
                continue
            raise SdcSyntaxError(
                f"unknown option -{key} to {cmd.name}", cmd.line, filename
            )
        positional.append(w)
        i += 1
    return opts, positional


def _build(commands: list[Command], design: SdcDesign, parser: _Parser, strict: bool) -> None:
    for cmd in commands:
        design.commands.append(cmd)
        name = cmd.name
        fn = _HANDLERS.get(name)
        if fn is None:
            if name in PASSTHROUGH_COMMANDS:
                continue
            if strict:
                raise SdcUnknownCommandError(
                    f"unknown SDC command {name!r}", cmd.line, design.filename
                )
            continue
        fn(cmd, design, parser)


def _h_create_clock(cmd: Command, d: SdcDesign, p: _Parser) -> None:
    opts, positional = _take_options(cmd, ["name", "period", "waveform"], ["add"], d.filename)
    if "period" not in opts:
        raise SdcSyntaxError("create_clock requires -period", cmd.line, d.filename)
    period = _as_float(opts["period"], "create_clock -period", cmd.line, d.filename)
    if period <= 0:
        raise SdcEvaluationError(
            f"create_clock -period must be positive, got {period}", cmd.line, d.filename
        )
    target = positional[0].as_scalar() if positional else None
    name = opts.get("name")
    if name is None:
        name = _default_clock_name(target)
    waveform = None
    if "waveform" in opts:
        waveform = tuple(
            _as_float(v, "create_clock -waveform", cmd.line, d.filename)
            for v in str(opts["waveform"]).split()
        )
    d.clocks.append(
        Clock(
            name=str(name),
            period_ns=period,
            target=target,
            waveform=waveform,
            line=cmd.line,
        )
    )


def _h_create_generated_clock(cmd: Command, d: SdcDesign, p: _Parser) -> None:
    opts, positional = _take_options(
        cmd, ["name", "source", "divide_by", "multiply_by"], ["add", "master_clock"], d.filename
    )
    if "divide_by" not in opts and "multiply_by" not in opts:
        raise SdcSyntaxError(
            "create_generated_clock requires -divide_by or -multiply_by",
            cmd.line,
            d.filename,
        )
    if "divide_by" in opts and "multiply_by" in opts:
        raise SdcSyntaxError(
            "create_generated_clock accepts -divide_by or -multiply_by, not both",
            cmd.line,
            d.filename,
        )
    name = opts.get("name")
    target = positional[0].as_scalar() if positional else None
    if name is None:
        name = _default_clock_name(target)
    d.generated_clocks.append(
        GeneratedClock(
            name=str(name),
            source=opts.get("source"),
            target=target,
            divide_by=_as_int(opts["divide_by"], "-divide_by", cmd.line, d.filename)
            if "divide_by" in opts
            else None,
            multiply_by=_as_int(opts["multiply_by"], "-multiply_by", cmd.line, d.filename)
            if "multiply_by" in opts
            else None,
            line=cmd.line,
        )
    )


def _h_io_delay(kind: str):
    def handler(cmd: Command, d: SdcDesign, p: _Parser) -> None:
        opts, positional = _take_options(cmd, ["clock", "max", "min"], [], d.filename)
        if not positional:
            raise SdcSyntaxError(
                f"{cmd.name} requires a target object", cmd.line, d.filename
            )
        if "max" not in opts and "min" not in opts:
            raise SdcSyntaxError(
                f"{cmd.name} requires -max and/or -min", cmd.line, d.filename
            )
        obj = IoDelay(
            kind=kind,
            target=positional[0].as_scalar(),
            clock=opts.get("clock"),
            max_ns=_as_float(opts["max"], f"{cmd.name} -max", cmd.line, d.filename)
            if "max" in opts
            else None,
            min_ns=_as_float(opts["min"], f"{cmd.name} -min", cmd.line, d.filename)
            if "min" in opts
            else None,
            line=cmd.line,
        )
        (d.input_delays if kind == "input" else d.output_delays).append(obj)

    return handler


def _h_set_max_delay(cmd: Command, d: SdcDesign, p: _Parser) -> None:
    opts, positional = _take_options(cmd, ["from", "to", "through"], [], d.filename)
    if not positional:
        raise SdcSyntaxError("set_max_delay requires a delay value", cmd.line, d.filename)
    d.max_delays.append(
        MaxDelay(
            value_ns=_as_float(positional[0].as_scalar(), "set_max_delay", cmd.line, d.filename),
            frm=opts.get("from"),
            to=opts.get("to"),
            through=opts.get("through"),
            line=cmd.line,
        )
    )


def _h_set_false_path(cmd: Command, d: SdcDesign, p: _Parser) -> None:
    opts, _ = _take_options(cmd, ["from", "to", "through"], [], d.filename)
    d.false_paths.append(
        FalsePath(
            frm=opts.get("from"),
            to=opts.get("to"),
            through=opts.get("through"),
            line=cmd.line,
        )
    )


def _h_set_multicycle_path(cmd: Command, d: SdcDesign, p: _Parser) -> None:
    opts, positional = _take_options(cmd, ["from", "to", "through", "setup", "hold"], [], d.filename)
    if "setup" not in opts and "hold" not in opts:
        raise SdcSyntaxError(
            "set_multicycle_path requires -setup and/or -hold", cmd.line, d.filename
        )
    d.multicycle_paths.append(
        MulticyclePath(
            frm=opts.get("from"),
            to=opts.get("to"),
            setup=_as_int(opts["setup"], "-setup", cmd.line, d.filename)
            if "setup" in opts
            else None,
            hold=_as_int(opts["hold"], "-hold", cmd.line, d.filename) if "hold" in opts else None,
            line=cmd.line,
        )
    )


def _h_set_clock_groups(cmd: Command, d: SdcDesign, p: _Parser) -> None:
    modes = ["asynchronous", "exclusive", "physically_exclusive", "logically_exclusive"]
    # Walk the words by hand: `-group` may appear several times, so a simple
    # option dict (which would keep only the last one) is the wrong tool here.
    mode: str | None = None
    groups: list[list[str]] = []
    i = 0
    args = cmd.args
    while i < len(args):
        text = args[i].as_text().strip()
        if text.startswith("-") and text[1:] in modes:
            if mode is not None and mode != text[1:]:
                raise SdcSyntaxError(
                    f"set_clock_groups got conflicting modes -{mode} and {text}",
                    cmd.line,
                    d.filename,
                )
            mode = text[1:]
            i += 1
            continue
        if text == "-group":
            if i + 1 >= len(args):
                raise SdcSyntaxError(
                    "set_clock_groups -group requires a value", cmd.line, d.filename
                )
            members = args[i + 1].as_text().split()
            if not members:
                raise SdcSyntaxError(
                    "set_clock_groups -group must not be empty", cmd.line, d.filename
                )
            groups.append(members)
            i += 2
            continue
        if text.startswith("-group"):
            body = text[len("-group") :].strip()
            if body.startswith("{") and body.endswith("}"):
                body = body[1:-1]
            groups.append(body.split())
            i += 1
            continue
        raise SdcSyntaxError(
            f"unexpected argument {text!r} to set_clock_groups", cmd.line, d.filename
        )

    if mode is None:
        raise SdcSyntaxError(
            "set_clock_groups requires one of -asynchronous/-exclusive/"
            "-physically_exclusive/-logically_exclusive",
            cmd.line,
            d.filename,
        )
    if not groups:
        raise SdcSyntaxError("set_clock_groups requires at least one -group", cmd.line, d.filename)
    d.clock_groups.append(ClockGroup(mode=mode, groups=groups, line=cmd.line))


def _h_set(cmd: Command, d: SdcDesign, p: _Parser) -> None:
    if not cmd.args:
        raise SdcSyntaxError("set requires a variable name", cmd.line, d.filename)
    varname = cmd.args[0].as_text()
    value: Any = ""
    if len(cmd.args) > 1:
        value = cmd.args[1].as_scalar()
    p.variables[varname] = value
    d.variables[varname] = value


_HANDLERS = {
    "create_clock": _h_create_clock,
    "create_generated_clock": _h_create_generated_clock,
    "set_input_delay": _h_io_delay("input"),
    "set_output_delay": _h_io_delay("output"),
    "set_max_delay": _h_set_max_delay,
    "set_false_path": _h_set_false_path,
    "set_multicycle_path": _h_set_multicycle_path,
    "set_clock_groups": _h_set_clock_groups,
    "set": _h_set,
}


def _default_clock_name(target: Any) -> str:
    """Mimic a tool's auto-naming when ``-name`` is omitted."""
    if isinstance(target, Selector) and target.patterns:
        return target.patterns[0]
    if isinstance(target, str) and target:
        return target
    return "clk"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(text: str, filename: str | None = None, strict: bool = True) -> SdcDesign:
    """Parse SDC source text into an :class:`SdcDesign`.

    :param strict: when True (default) an unrecognised command is an error;
        when False it is ignored, which is useful for digesting constraint
        files written for a fuller tool.
    """
    parser = _Parser(text, filename)
    commands = parser.parse_script()
    design = SdcDesign(filename=filename)
    _build(commands, design, parser, strict)
    design.variables = dict(parser.variables)
    return design


def parse_file(path: str | os.PathLike[str], strict: bool = True) -> SdcDesign:
    """Read *path* as UTF-8 and parse it."""
    p = os.fspath(path)
    with open(p, "r", encoding="utf-8") as fh:
        return parse(fh.read(), filename=p, strict=strict)
