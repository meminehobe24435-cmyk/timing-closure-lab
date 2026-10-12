"""tclab.report_parse -- reader for nextpnr's JSON timing/utilization report.

The schema below was taken from a real ``nextpnr-ecp5 --report ... 
--detailed-timing-report`` file produced by this lab, not from documentation::

    {
      "fmax":                { "<clock>": {"achieved": <MHz>, "constraint": <MHz>} },
      "utilization":         { "<cell type>": {"used": int, "available": int} },
      "critical_paths":      [ {"from": <event>, "to": <event>, "path": [ <step>, ... ]} ],
      "detailed_net_timings":[ {"driver","net","port","event","sources",
                                "endpoints":[{"cell","port","delay":[min,max],"event"}]} ]
    }

A *step* looks like::

    {"type": "clk-to-q" | "logic" | "routing" | "setup" | "hold" | "source",
     "delay": <incremental ns>,
     "from": {"cell": str, "loc": [x, y], "port": str},
     "to":   {"cell": str, "loc": [x, y], "port": str},
     "net": str, "sources": [str]}

``delay`` is the *incremental* delay of that step; the path's total delay is the
sum over steps. That identity was verified against the tool: summing the steps
of the worst critical path reproduces ``1e3 / fmax.achieved`` to full precision.

Every reader here is defensive: a report missing ``detailed_net_timings``, with
an empty ``critical_paths`` list, or with ``null`` where an object is expected
must not raise. Degenerate inputs are normal in a flow that runs before timing
converges.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ReportError",
    "Utilization",
    "FmaxEntry",
    "PathStep",
    "TimingPath",
    "NetEndpoint",
    "NetTiming",
    "NextpnrReport",
    "parse_report",
    "parse_report_file",
    "normalize_cell_name",
    "CELL_SUFFIXES",
]

# ---------------------------------------------------------------------------
# Cell-name normalisation
# ---------------------------------------------------------------------------

#: nextpnr flattens the design and encodes the technology cell in the instance
#: name, e.g. ``u_core.sr[11]_TRELLIS_FF_Q_3``. Stripping that suffix recovers
#: the RTL-level signal, which is what a human reading the report wants.
CELL_SUFFIXES = (
    "TRELLIS_FF",
    "TRELLIS_COMB",
    "TRELLIS_IO",
    "TRELLIS_RAMW",
    "MULT18X18D",
    "ALU54B",
    "DP16KD",
    "CLKDIVF",
    "L6MUX21",
    "PFUMX",
    "CCU2C",
    "LUT4",
    "LUT2",
    "LUT1",
)

_CELL_SUFFIX_RE = re.compile(r"_(?:" + "|".join(CELL_SUFFIXES) + r")(?![A-Za-z0-9])")
_IO_SUFFIX_RE = re.compile(r"\$tr_io$")


def normalize_cell_name(name: str | None) -> str:
    """Map a flattened nextpnr cell name back to an RTL-ish signal name.

    ``u_core.sr[11]_TRELLIS_FF_Q_3``      -> ``u_core.sr[11]``
    ``u_core.g_chain.chain[15]_CCU2C_S1_7$CCU2_COMB1`` -> ``u_core.g_chain.chain[15]``
    ``dout[0]$tr_io``                     -> ``dout[0]``
    ``clk$TRELLIS_IO_IN``                 -> ``clk``
    """
    if not name:
        return ""
    s = str(name)
    s = _IO_SUFFIX_RE.sub("", s)
    m = _CELL_SUFFIX_RE.search(s)
    if m:
        s = s[: m.start()]
    # Trailing ``$``-suffixed tool annotations (e.g. ``$CCU2_FCI_INT``).
    if "$" in s:
        head, _, tail = s.rpartition("$")
        if head:
            s = head
    return s.strip()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReportError(Exception):
    """Raised only for a report that cannot be read at all (missing/bad JSON).

    Structurally odd but readable reports do not raise; missing members degrade
    to empty collections so a partially written report still yields a summary.
    """


# ---------------------------------------------------------------------------
# Utilisation
# ---------------------------------------------------------------------------


@dataclass
class Utilization:
    """Resource usage as reported by the placer/router."""

    cells: dict[str, tuple[int, int]] = field(default_factory=dict)

    def used(self, cell: str) -> int:
        ent = self.cells.get(cell)
        return int(ent[0]) if ent else 0

    def available(self, cell: str) -> int:
        ent = self.cells.get(cell)
        return int(ent[1]) if ent else 0

    def pct(self, cell: str) -> float:
        avail = self.available(cell)
        return 100.0 * self.used(cell) / avail if avail else 0.0

    @property
    def lut4(self) -> int:
        """Logic LUT4 count (``TRELLIS_COMB``), the ECP5 logic primitive."""
        return self.used("TRELLIS_COMB")

    @property
    def ff(self) -> int:
        return self.used("TRELLIS_FF")

    @property
    def bram(self) -> int:
        return self.used("DP16KD")

    @property
    def dsp(self) -> int:
        return self.used("MULT18X18D")

    @property
    def io(self) -> int:
        return self.used("TRELLIS_IO")

    def nonzero(self) -> dict[str, tuple[int, int]]:
        return {k: v for k, v in sorted(self.cells.items()) if v[0]}

    def as_dict(self) -> dict[str, Any]:
        return {
            "lut4": self.lut4,
            "ff": self.ff,
            "bram": self.bram,
            "dsp": self.dsp,
            "io": self.io,
            "lut4_available": self.available("TRELLIS_COMB"),
            "ff_available": self.available("TRELLIS_FF"),
            "cells_nonzero": {k: v[0] for k, v in self.nonzero().items()},
        }


# ---------------------------------------------------------------------------
# Fmax
# ---------------------------------------------------------------------------


@dataclass
class FmaxEntry:
    """One row of the tool's ``fmax`` block: achieved vs constrained."""

    clock: str
    achieved_mhz: float
    constraint_mhz: float

    @property
    def achieved_period_ns(self) -> float:
        return 1000.0 / self.achieved_mhz if self.achieved_mhz > 0 else float("inf")

    @property
    def is_met(self) -> bool:
        return self.achieved_mhz >= self.constraint_mhz

    @property
    def margin_mhz(self) -> float:
        return self.achieved_mhz - self.constraint_mhz


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass
class PathStep:
    """One incremental step along a timing path."""

    kind: str
    delay_ns: float
    from_cell: str = ""
    from_port: str = ""
    to_cell: str = ""
    to_port: str = ""
    net: str = ""
    sources: tuple[str, ...] = ()

    @property
    def is_routing(self) -> bool:
        return self.kind == "routing"

    @property
    def rtl_source(self) -> str:
        """First RTL source annotation attached to this step, if any."""
        return self.sources[0] if self.sources else ""


@dataclass
class TimingPath:
    """A full path between two clock events (or an I/O boundary)."""

    frm: str
    to: str
    steps: list[PathStep] = field(default_factory=list)

    @property
    def total_ns(self) -> float:
        return sum(s.delay_ns for s in self.steps)

    @property
    def is_clocked(self) -> bool:
        """True for a register-to-register path inside one clock domain."""
        return self.frm.startswith("posedge") and self.to.startswith("posedge") and self.frm == self.to

    @property
    def start_point(self) -> str:
        """RTL-ish name of the launching flop (or the I/O port for a source)."""
        for s in self.steps:
            if s.kind in ("clk-to-q", "source"):
                return normalize_cell_name(s.from_cell)
        return normalize_cell_name(self.steps[0].from_cell) if self.steps else ""

    @property
    def end_point(self) -> str:
        """RTL-ish name of the capturing flop (or the output port)."""
        for s in reversed(self.steps):
            if s.kind in ("setup", "hold"):
                return normalize_cell_name(s.to_cell)
        return normalize_cell_name(self.steps[-1].to_cell) if self.steps else ""

    def breakdown(self) -> dict[str, float]:
        """Total nanoseconds per step kind."""
        out: dict[str, float] = {}
        for s in self.steps:
            out[s.kind] = out.get(s.kind, 0.0) + s.delay_ns
        return out

    def delay_types(self) -> dict[str, float]:
        """Coarser split into the three quantities STA reports care about."""
        b = self.breakdown()
        return {
            "logic_ns": b.get("logic", 0.0),
            "routing_ns": b.get("routing", 0.0),
            "clk_to_q_ns": b.get("clk-to-q", 0.0),
            "setup_ns": b.get("setup", 0.0),
            "source_ns": b.get("source", 0.0),
        }

    @property
    def logic_ns(self) -> float:
        return self.breakdown().get("logic", 0.0)

    @property
    def routing_ns(self) -> float:
        return self.breakdown().get("routing", 0.0)

    @property
    def clk_to_q_ns(self) -> float:
        return self.breakdown().get("clk-to-q", 0.0)

    @property
    def implied_fmax_mhz(self) -> float:
        t = self.total_ns
        return 1000.0 / t if t > 0 else float("inf")

    def top_steps(self, n: int = 10) -> list[PathStep]:
        return sorted(self.steps, key=lambda s: s.delay_ns, reverse=True)[:n]

    def culprit_nets(self, n: int = 10) -> list[tuple[str, float]]:
        """Routing steps ranked by delay -- the nets that actually cost time."""
        r = [(s.net, s.delay_ns) for s in self.steps if s.kind == "routing" and s.net]
        r.sort(key=lambda x: x[1], reverse=True)
        return r[:n]

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_event": self.frm,
            "to_event": self.to,
            "start_point": self.start_point,
            "end_point": self.end_point,
            "total_ns": self.total_ns,
            "implied_fmax_mhz": self.implied_fmax_mhz,
            "steps": len(self.steps),
            "by_type": self.delay_types(),
        }


# ---------------------------------------------------------------------------
# Net timings
# ---------------------------------------------------------------------------


@dataclass
class NetEndpoint:
    cell: str
    port: str = ""
    min_ns: float | None = None
    max_ns: float | None = None
    event: str = ""

    @property
    def rtl_cell(self) -> str:
        return normalize_cell_name(self.cell)


@dataclass
class NetTiming:
    driver: str = ""
    net: str = ""
    port: str = ""
    event: str = ""
    sources: tuple[str, ...] = ()
    endpoints: list[NetEndpoint] = field(default_factory=list)

    @property
    def fanout(self) -> int:
        return len(self.endpoints)

    @property
    def worst_arrival_ns(self) -> float | None:
        vals = [e.max_ns for e in self.endpoints if e.max_ns is not None]
        return max(vals) if vals else None

    @property
    def rtl_driver(self) -> str:
        return normalize_cell_name(self.driver)


# ---------------------------------------------------------------------------
# Whole report
# ---------------------------------------------------------------------------


@dataclass
class NextpnrReport:
    """Everything readable out of one nextpnr JSON report."""

    source: str | None = None
    fmax: list[FmaxEntry] = field(default_factory=list)
    utilization: Utilization = field(default_factory=Utilization)
    critical_paths: list[TimingPath] = field(default_factory=list)
    net_timings: list[NetTiming] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- queries ------------------------------------------------------------

    def clocked_paths(self) -> list[TimingPath]:
        """Register-to-register paths only; excludes I/O and reset paths."""
        return [p for p in self.critical_paths if p.is_clocked]

    def worst_clocked_path(self) -> TimingPath | None:
        """The path that sets the achievable clock period."""
        paths = self.clocked_paths()
        if not paths:
            return None
        return max(paths, key=lambda p: p.total_ns)

    def fmax_for(self, clock: str) -> FmaxEntry | None:
        for e in self.fmax:
            if e.clock == clock:
                return e
        return None

    @property
    def primary_fmax(self) -> FmaxEntry | None:
        """The entry that corresponds to the design's main clock.

        nextpnr reports clocks under their routed global-net name
        (``$glbnet$clk$TRELLIS_IO_IN``). We pick the fastest-constrained entry,
        which is the datapath clock rather than the divided readout clock.
        """
        if not self.fmax:
            return None
        return max(self.fmax, key=lambda e: e.constraint_mhz)

    def worst_arrival_by_endpoint(self, n: int = 10) -> list[tuple[str, float, int]]:
        """Top-N capture points by worst max-delay arrival.

        Returns ``(rtl_cell, worst_arrival_ns, fanin_count)`` sorted by delay.
        """
        agg: dict[str, tuple[float, int]] = {}
        for nt in self.net_timings:
            for ep in nt.endpoints:
                if ep.max_ns is None:
                    continue
                cell = ep.rtl_cell
                prev = agg.get(cell)
                if prev is None:
                    agg[cell] = (ep.max_ns, 1)
                else:
                    agg[cell] = (max(prev[0], ep.max_ns), prev[1] + 1)
        out = [(c, v[0], v[1]) for c, v in agg.items()]
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:n]

    def worst_drivers(self, n: int = 10) -> list[tuple[str, float, int]]:
        """Top-N nets by (worst max-delay at any sink, fanout).

        These are the nets whose routing delay is eating the clock period.
        """
        out: list[tuple[str, float, int]] = []
        for nt in self.net_timings:
            w = nt.worst_arrival_ns
            if w is None:
                continue
            label = nt.rtl_driver or nt.net
            out.append((label, w, nt.fanout))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:n]

    def total_nets(self) -> int:
        return len(self.net_timings)

    def is_empty(self) -> bool:
        return not (self.fmax or self.critical_paths or self.net_timings or self.utilization.cells)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _as_float(v: Any, default: float | None = None) -> float | None:
    """Coerce to a finite float, else *default*.

    ``float("nan")`` and ``float("inf")`` both parse successfully, so a report
    carrying "NaN" would otherwise inject a non-finite delay into every
    downstream sum and silently poison the slack arithmetic. A non-finite delay
    is a corrupt field, not a very large one, so it degrades to the default.
    """
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        out = float(v)
    elif isinstance(v, str):
        try:
            out = float(v)
        except ValueError:
            return default
    else:
        return default
    return out if math.isfinite(out) else default


def _as_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return str(v)


def _as_list(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    return []


def _as_dict(v: Any) -> Mapping[str, Any]:
    return v if isinstance(v, Mapping) else {}


def _parse_point(v: Any) -> tuple[str, str]:
    d = _as_dict(v)
    return _as_str(d.get("cell")), _as_str(d.get("port"))


def _parse_step(raw: Any) -> PathStep | None:
    d = _as_dict(raw)
    if not d:
        return None
    fcell, fport = _parse_point(d.get("from"))
    tcell, tport = _parse_point(d.get("to"))
    delay = _as_float(d.get("delay"), 0.0) or 0.0
    srcs = tuple(_as_str(s) for s in _as_list(d.get("sources")) if isinstance(s, str))
    return PathStep(
        kind=_as_str(d.get("type"), "unknown") or "unknown",
        delay_ns=delay,
        from_cell=fcell,
        from_port=fport,
        to_cell=tcell,
        to_port=tport,
        net=_as_str(d.get("net")),
        sources=srcs,
    )


def _parse_path(raw: Any) -> TimingPath | None:
    d = _as_dict(raw)
    if not d:
        return None
    steps = [s for s in (_parse_step(x) for x in _as_list(d.get("path"))) if s is not None]
    return TimingPath(frm=_as_str(d.get("from")), to=_as_str(d.get("to")), steps=steps)


def _parse_net_timing(raw: Any) -> NetTiming | None:
    d = _as_dict(raw)
    if not d:
        return None
    eps: list[NetEndpoint] = []
    for e in _as_list(d.get("endpoints")):
        ed = _as_dict(e)
        if not ed:
            continue
        dly = ed.get("delay")
        mn = mx = None
        if isinstance(dly, (list, tuple)):
            if len(dly) >= 1:
                mn = _as_float(dly[0])
            if len(dly) >= 2:
                mx = _as_float(dly[1])
            elif len(dly) == 1:
                mx = mn
        else:
            mx = _as_float(dly)
            mn = mx
        eps.append(
            NetEndpoint(
                cell=_as_str(ed.get("cell")),
                port=_as_str(ed.get("port")),
                min_ns=mn,
                max_ns=mx,
                event=_as_str(ed.get("event")),
            )
        )
    srcs = tuple(_as_str(s) for s in _as_list(d.get("sources")) if isinstance(s, str))
    return NetTiming(
        driver=_as_str(d.get("driver")),
        net=_as_str(d.get("net")),
        port=_as_str(d.get("port")),
        event=_as_str(d.get("event")),
        sources=srcs,
        endpoints=eps,
    )


def _parse_utilization(raw: Any) -> Utilization:
    cells: dict[str, tuple[int, int]] = {}
    for name, ent in _as_dict(raw).items():
        d = _as_dict(ent)
        if not d:
            continue
        used = _as_float(d.get("used"), 0.0) or 0.0
        avail = _as_float(d.get("available"), 0.0) or 0.0
        cells[str(name)] = (int(used), int(avail))
    return Utilization(cells=cells)


def _parse_fmax(raw: Any) -> list[FmaxEntry]:
    out: list[FmaxEntry] = []
    for clock, ent in _as_dict(raw).items():
        d = _as_dict(ent)
        if not d:
            continue
        achieved = _as_float(d.get("achieved"), 0.0) or 0.0
        constraint = _as_float(d.get("constraint"), 0.0) or 0.0
        out.append(
            FmaxEntry(clock=str(clock), achieved_mhz=achieved, constraint_mhz=constraint)
        )
    out.sort(key=lambda e: e.clock)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_report(data: Mapping[str, Any] | str | bytes, source: str | None = None) -> NextpnrReport:
    """Build a :class:`NextpnrReport` from a mapping or a JSON document.

    Never raises for structurally odd input; only genuinely unparseable JSON
    raises :class:`ReportError`.
    """
    if isinstance(data, (str, bytes)):
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReportError(f"not valid JSON: {exc}") from exc
        if not isinstance(obj, Mapping):
            raise ReportError(f"report root must be an object, got {type(obj).__name__}")
        root: Mapping[str, Any] = obj
    elif isinstance(data, Mapping):
        root = data
    else:
        raise ReportError(f"unsupported report input type: {type(data).__name__}")

    paths = [p for p in (_parse_path(x) for x in _as_list(root.get("critical_paths"))) if p]
    nets = [
        n
        for n in (_parse_net_timing(x) for x in _as_list(root.get("detailed_net_timings")))
        if n
    ]
    return NextpnrReport(
        source=source,
        fmax=_parse_fmax(root.get("fmax")),
        utilization=_parse_utilization(root.get("utilization")),
        critical_paths=paths,
        net_timings=nets,
        raw=dict(root),
    )


def parse_report_file(path: str | os.PathLike[str]) -> NextpnrReport:
    """Read and parse a nextpnr ``--report`` JSON file."""
    p = os.fspath(path)
    if not os.path.exists(p):
        raise ReportError(f"report file not found: {p}")
    with open(p, "r", encoding="utf-8") as fh:
        text = fh.read()
    return parse_report(text, source=p)
