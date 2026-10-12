"""tclab.sta -- static timing analysis over a parsed nextpnr report.

This layer turns raw nextpnr output into the quantities an STA engineer reads
off a report:

  * achieved vs constrained Fmax per clock, and the setup slack that falls out
  * the critical path, with its delay split into clk-to-q / logic / routing
  * the nets and capture points that actually set the period, ranked
  * a short, generated conclusion saying which paths are blocking closure

Scope and honesty
-----------------
*What this does*: setup (max-delay) analysis, derived from the tool's own
worst-path extraction, plus min/max arrival data from ``detailed_net_timings``.

*What this does not do*: it is not signoff STA. There is no process library, no
SPEF/parasitic back-annotation, no crosstalk, no OCV/AOCV derating, and no
hold-time requirement to check. nextpnr's ECP5 flow reports max-delay setup
analysis only; there is no hold requirement in the report to compare against,
so this module reports min-delay data where it exists and marks hold as
``not analysed`` rather than inventing a number.

The margin figure is therefore "slack against the constraint the tool was
given", which is the right thing to close a design against, not a signoff
sign-off number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .report_parse import FmaxEntry, NextpnrReport, TimingPath

__all__ = [
    "fmax_mhz",
    "period_ns",
    "setup_slack_ns",
    "ClockAnalysis",
    "Bottleneck",
    "StaSummary",
    "analyze",
]


# ---------------------------------------------------------------------------
# Small scalar helpers (independently testable)
# ---------------------------------------------------------------------------


def fmax_mhz(path_delay_ns: float) -> float:
    """Frequency implied by a combinational path of *path_delay_ns*.

    ``inf`` for a non-positive delay, because a zero-delay path does not bound
    the clock period.
    """
    if path_delay_ns is None or path_delay_ns <= 0:
        return float("inf")
    return 1000.0 / path_delay_ns


def period_ns(frequency_mhz: float) -> float:
    """Clock period in nanoseconds for a frequency in MHz."""
    if frequency_mhz <= 0:
        return float("inf")
    return 1000.0 / frequency_mhz


def setup_slack_ns(required_period_ns: float, path_delay_ns: float) -> float:
    """Setup slack = required period - actual worst path delay.

    Positive means the path closes at that period; negative means it does not.
    """
    return required_period_ns - path_delay_ns


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ClockAnalysis:
    """Per-clock timing status."""

    clock: str
    constraint_mhz: float
    achieved_mhz: float
    worst_path_ns: float

    @property
    def constraint_period_ns(self) -> float:
        return period_ns(self.constraint_mhz)

    @property
    def achieved_period_ns(self) -> float:
        return period_ns(self.achieved_mhz)

    @property
    def setup_slack_ns(self) -> float:
        """Slack of the worst path against the *constrained* period."""
        return setup_slack_ns(self.constraint_period_ns, self.worst_path_ns)

    @property
    def met(self) -> bool:
        return self.setup_slack_ns >= 0.0

    @property
    def headroom_mhz(self) -> float:
        return self.achieved_mhz - self.constraint_mhz

    def as_dict(self) -> dict[str, Any]:
        return {
            "clock": self.clock,
            "constraint_mhz": self.constraint_mhz,
            "achieved_mhz": self.achieved_mhz,
            "worst_path_ns": self.worst_path_ns,
            "constraint_period_ns": self.constraint_period_ns,
            "achieved_period_ns": self.achieved_period_ns,
            "setup_slack_ns": self.setup_slack_ns,
            "met": self.met,
        }


@dataclass
class Bottleneck:
    """One entry of a ranked timing-bottleneck list."""

    name: str
    worst_arrival_ns: float
    count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "worst_arrival_ns": self.worst_arrival_ns,
            "count": self.count,
        }


@dataclass
class StaSummary:
    """The full picture for one place-and-route run."""

    clocks: list[ClockAnalysis] = field(default_factory=list)
    critical_path: dict[str, Any] | None = None
    bottlenecks_by_endpoint: list[Bottleneck] = field(default_factory=list)
    bottlenecks_by_start: list[Bottleneck] = field(default_factory=list)
    nets_by_delay: list[Bottleneck] = field(default_factory=list)
    delay_split: dict[str, float] = field(default_factory=dict)
    hold_analysed: bool = False
    hold_note: str = ""
    conclusions: list[str] = field(default_factory=list)
    utilization: dict[str, Any] = field(default_factory=dict)

    @property
    def primary(self) -> ClockAnalysis | None:
        if not self.clocks:
            return None
        return max(self.clocks, key=lambda c: c.constraint_mhz)

    @property
    def worst_achieved_mhz(self) -> float:
        """Achievable frequency of the design's main datapath clock."""
        p = self.primary
        return p.achieved_mhz if p else 0.0

    @property
    def timing_met(self) -> bool:
        return all(c.met for c in self.clocks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "clocks": [c.as_dict() for c in self.clocks],
            "critical_path": self.critical_path,
            "bottlenecks_by_endpoint": [b.as_dict() for b in self.bottlenecks_by_endpoint],
            "bottlenecks_by_start": [b.as_dict() for b in self.bottlenecks_by_start],
            "nets_by_delay": [b.as_dict() for b in self.nets_by_delay],
            "delay_split_ns": self.delay_split,
            "hold_analysed": self.hold_analysed,
            "hold_note": self.hold_note,
            "conclusions": self.conclusions,
            "utilization": self.utilization,
            "worst_achieved_mhz": self.worst_achieved_mhz,
            "timing_met": self.timing_met,
        }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _rank(pairs: Iterable[tuple[str, float, int]], n: int) -> list[Bottleneck]:
    best: dict[str, tuple[float, int]] = {}
    for name, val, cnt in pairs:
        if not name:
            continue
        prev = best.get(name)
        if prev is None:
            best[name] = (val, cnt)
        else:
            best[name] = (max(prev[0], val), prev[1] + cnt)
    out = [Bottleneck(name=k, worst_arrival_ns=v[0], count=v[1]) for k, v in best.items()]
    out.sort(key=lambda b: b.worst_arrival_ns, reverse=True)
    return out[:n]


def _conclusions(
    clocks: Sequence[ClockAnalysis],
    path: TimingPath | None,
    endpoints: Sequence[Bottleneck],
    drivers: Sequence[Bottleneck],
) -> list[str]:
    """Generate the 'what is blocking closure' sentences."""
    out: list[str] = []
    for c in clocks:
        if c.met:
            out.append(
                f"{c.clock}: MET - worst path {c.worst_path_ns:.3f} ns leaves "
                f"{c.setup_slack_ns:+.3f} ns of setup slack at {c.constraint_mhz:.3f} MHz "
                f"(achievable {c.achieved_mhz:.3f} MHz)."
            )
        else:
            out.append(
                f"{c.clock}: VIOLATED - worst path {c.worst_path_ns:.3f} ns needs "
                f"{c.constraint_period_ns:.3f} ns, so setup slack is "
                f"{c.setup_slack_ns:+.3f} ns at {c.constraint_mhz:.3f} MHz "
                f"(achievable {c.achieved_mhz:.3f} MHz)."
            )
    if path is not None:
        d = path.delay_types()
        total = path.total_ns
        if total > 0:
            rp = 100.0 * d["routing_ns"] / total
            lp = 100.0 * d["logic_ns"] / total
            out.append(
                f"Critical path {path.start_point} -> {path.end_point} is {total:.3f} ns "
                f"over {len(path.steps)} steps: {d['logic_ns']:.3f} ns logic ({lp:.1f}%), "
                f"{d['routing_ns']:.3f} ns routing ({rp:.1f}%), "
                f"{d['clk_to_q_ns']:.3f} ns clk-to-q."
            )
            if rp > 50.0:
                out.append(
                    "Routing dominates the critical path, so this is a placement/congestion "
                    "problem as much as a logic-depth problem."
                )
            else:
                out.append(
                    "Logic delay dominates the critical path, so shortening the "
                    "combinational depth is the lever that matters."
                )
    if drivers:
        top = drivers[0]
        out.append(
            f"Worst net by arrival is {top.name} at {top.worst_arrival_ns:.3f} ns "
            f"({top.count} sink(s)); the slowest nets are the ones to shorten first."
        )
    if endpoints:
        top = endpoints[0]
        out.append(
            f"Worst capture point is {top.name} at {top.worst_arrival_ns:.3f} ns "
            f"over {top.count} timed pin(s)."
        )
    return out


def analyze(
    report: NextpnrReport,
    top_n: int = 10,
    sdc: Any = None,
) -> StaSummary:
    """Analyse *report* and return a :class:`StaSummary`.

    :param top_n: how many entries to keep in each ranked bottleneck list.
    :param sdc: optional parsed :class:`tclab.sdc.SdcDesign`; when supplied its
        primary clock period is recorded for cross-checking the tool's
        constraint. (nextpnr only honours ``create_clock``, so for this lab the
        tool's own ``constraint`` value is the authoritative one.)
    """
    summary = StaSummary()
    summary.utilization = report.utilization.as_dict()

    worst = report.worst_clocked_path()

    # Per-clock status. nextpnr's `achieved` is the tool's own worst-path
    # result; we cross-check it against the extracted path and prefer the
    # larger of the two so a mis-extracted path cannot flatter the design.
    for e in report.fmax:
        path_ns = e.achieved_period_ns
        if worst is not None and _clock_matches(e, worst) and worst.total_ns > path_ns:
            path_ns = worst.total_ns
        summary.clocks.append(
            ClockAnalysis(
                clock=e.clock,
                constraint_mhz=e.constraint_mhz,
                achieved_mhz=fmax_mhz(path_ns),
                worst_path_ns=path_ns,
            )
        )

    if worst is not None:
        summary.critical_path = worst.as_dict()
        summary.critical_path["breakdown_ns"] = worst.breakdown()
        summary.critical_path["culprit_nets"] = [
            {"net": n, "delay_ns": d} for n, d in worst.culprit_nets(top_n)
        ]
        summary.delay_split = worst.delay_types()

    summary.bottlenecks_by_endpoint = [
        Bottleneck(name=c, worst_arrival_ns=v, count=n)
        for c, v, n in report.worst_arrival_by_endpoint(top_n)
    ]
    summary.bottlenecks_by_start = [
        Bottleneck(name=c, worst_arrival_ns=v, count=n)
        for c, v, n in report.worst_drivers(top_n)
    ]

    route_pairs: list[tuple[str, float, int]] = []
    for nt in report.net_timings:
        for ep in nt.endpoints:
            if ep.max_ns is None or not nt.port:
                continue
            route_pairs.append((nt.rtl_driver or nt.net, ep.max_ns, 1))
    summary.nets_by_delay = _rank(route_pairs, top_n)

    summary.hold_analysed = False
    summary.hold_note = (
        "nextpnr's ECP5 flow performs max-delay (setup) analysis only; it reports no "
        "hold requirement, so no hold slack is claimed here. Min-delay arrival values "
        "are available in the raw report for inspection."
    )

    summary.conclusions = _conclusions(
        summary.clocks, worst, summary.bottlenecks_by_endpoint, summary.bottlenecks_by_start
    )
    return summary


def _clock_matches(entry: FmaxEntry, path: TimingPath) -> bool:
    """Does this fmax entry describe the domain the critical path lives in?

    nextpnr names clocks after routed global nets, so an exact string match is
    not reliable; the clock name usually appears as a substring.
    """
    if entry.clock == path.frm:
        return True
    # ``$glbnet$clk$TRELLIS_IO_IN`` contains ``clk``; ``path.frm`` contains it too.
    return path.frm in entry.clock or entry.clock in path.frm
