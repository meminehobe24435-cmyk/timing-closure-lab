"""tclab.power -- VCD toggle-rate extraction and a *relative* power estimate.

Pipeline: ``iverilog``/``vvp`` runs an activity testbench that dumps a VCD, this
module counts how often every net actually switches, and a deliberately simple
switching-power model turns those counts into a comparable number.

The model
---------
Dynamic (switching) power of a net is ``P = alpha * C * V^2 * f``. With no
process library there is no ``C`` and no ``V``, so this module computes a
**relative** figure::

    P_rel = SUM_over_nets( class_weight(net) * bit_toggle_rate(net) )

where ``bit_toggle_rate`` is measured from the VCD (bit flips per second, summed
across the bits of a bus) and ``class_weight`` is a small, explicit, tunable
weight standing in for capacitance and swing. Everything is unitless and
normalised against a reference run.

What that buys: the *ratio* between two runs of the same design (full activity
vs reduced activity) is meaningful, because both runs share the same weights and
the same netlist.

What it does not buy, and this is the important caveat:

  * no process library -> no absolute capacitance, no absolute milliwatts
  * no clock-tree model, no leakage, no I/O power, no glitching power
  * not signoff power analysis; a real number needs a characterised library,
    real parasitics and a signoff tool

So every number this module produces is reported as a ratio, never as "mW".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

__all__ = [
    "VcdParseError",
    "VcdSignal",
    "ToggleStat",
    "VcdActivity",
    "parse_vcd",
    "parse_vcd_file",
    "PowerModel",
    "PowerEstimate",
    "estimate",
    "compare",
]


class VcdParseError(Exception):
    """The VCD is malformed beyond what the tolerant reader can skip."""


# ---------------------------------------------------------------------------
# VCD reading
# ---------------------------------------------------------------------------


@dataclass
class VcdSignal:
    """One ``$var`` declaration."""

    id: str
    name: str
    width: int = 1
    vcd_type: str = "wire"
    scope: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.scope}.{self.name}" if self.scope else self.name


@dataclass
class ToggleStat:
    """Switching activity measured for one signal."""

    name: str
    width: int
    value_changes: int = 0
    bit_toggles: int = 0
    high_time_units: int = 0

    def toggle_rate_hz(self, duration_ns: float) -> float:
        """Bit toggles per second."""
        if duration_ns <= 0:
            return 0.0
        return self.bit_toggles / (duration_ns * 1e-9)

    def activity_factor(self, duration_ns: float, clock_mhz: float) -> float:
        """alpha = bit_toggles / (2 * cycles), the classic activity factor."""
        cycles = (duration_ns * 1e-9) * (clock_mhz * 1e6)
        if cycles <= 0:
            return 0.0
        return self.bit_toggles / (2.0 * cycles)


@dataclass
class VcdActivity:
    """Everything extracted from one VCD dump."""

    source: str | None = None
    timescale_s: float = 1e-9
    end_time_units: int = 0
    signals: dict[str, VcdSignal] = field(default_factory=dict)
    stats: dict[str, ToggleStat] = field(default_factory=dict)

    @property
    def duration_ns(self) -> float:
        return self.end_time_units * self.timescale_s * 1e9

    @property
    def total_bit_toggles(self) -> int:
        return sum(s.bit_toggles for s in self.stats.values())

    @property
    def total_value_changes(self) -> int:
        return sum(s.value_changes for s in self.stats.values())

    def by_name(self, name: str) -> ToggleStat | None:
        for s in self.stats.values():
            if s.name == name:
                return s
        return None

    def top_toggling(self, n: int = 10) -> list[ToggleStat]:
        return sorted(self.stats.values(), key=lambda s: s.bit_toggles, reverse=True)[:n]

    def toggling_only(self, minimum: int = 1) -> list[ToggleStat]:
        return [s for s in self.stats.values() if s.bit_toggles >= minimum]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "duration_ns": self.duration_ns,
            "end_time_units": self.end_time_units,
            "timescale_s": self.timescale_s,
            "n_signals": len(self.signals),
            "total_value_changes": self.total_value_changes,
            "total_bit_toggles": self.total_bit_toggles,
            "signals_toggling": len(self.toggling_only()),
            "top_toggling": [
                {
                    "name": s.name,
                    "width": s.width,
                    "bit_toggles": s.bit_toggles,
                    "value_changes": s.value_changes,
                    "toggle_rate_hz": s.toggle_rate_hz(self.duration_ns),
                }
                for s in self.top_toggling(10)
            ],
        }


_TS_RE = re.compile(r"^\s*([0-9]+)\s*(fs|ps|ns|us|ms|s)\s*$")
_TS_FACTOR = {
    "fs": 1e-15,
    "ps": 1e-12,
    "ns": 1e-9,
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
}


def _bit_count(value: str) -> int | None:
    """Number of set bits in a VCD binary value, or None if it has x/z."""
    if any(c in value for c in "xzXZ"):
        return None
    return value.count("1")


def _popcount_diff(old: str, new: str) -> int:
    """Bit flips between two VCD values, right-aligned.

    Returns 0 when either value contains x/z, because an unknown transition is
    not a switching event that can be honestly counted.
    """
    if old is None:
        return 0
    if any(c in old for c in "xzXZ") or any(c in new for c in "xzXZ"):
        return 0
    o = old.rjust(len(new), "0")
    n = new.rjust(len(o), "0")
    width = max(len(o), len(n))
    o = o.rjust(width, "0")
    n = n.rjust(width, "0")
    return sum(1 for a, b in zip(o, n) if a != b)


def parse_vcd(text: str, source: str | None = None, max_changes: int | None = None) -> VcdActivity:
    """Parse VCD text, counting activity rather than storing waveforms.

    Only toggle counts are retained, so memory is O(number of signals) instead
    of O(number of transitions). That matters because a realistic dump of this
    design is tens of megabytes.

    :param max_changes: stop after this many value changes (a safety valve for
        enormous dumps); the returned ``end_time_units`` is then a truncation
        point, which is still a valid sample window.
    """
    act = VcdActivity(source=source)
    ids: dict[str, VcdSignal] = {}
    scopes: list[str] = []
    last: dict[str, str] = {}
    changes = 0
    time_units = 0
    i = 0
    n = len(text)
    in_header = True
    # True between `$dumpvars` and its `$end`: values seen there are initial
    # conditions, recorded but not counted as switching activity.
    initialising = False

    def read_token() -> str:
        nonlocal i
        while i < n and text[i] in " \t\r\n":
            i += 1
        start = i
        while i < n and text[i] not in " \t\r\n":
            i += 1
        return text[start:i]

    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue

        if ch == "$":
            # A header/control command: read the keyword, then its arguments up
            # to the matching $end.
            kw = read_token()[1:]

            # ``$end`` terminates a block; it takes no arguments, so it must not
            # enter the argument-reading loop below (which would then swallow
            # everything up to the *next* $end in the file).
            if kw == "end":
                initialising = False
                continue

            # ``$dumpvars`` / ``$dumpon`` bodies are value changes, not
            # arguments, and are handled by the value-change branch below. Their
            # initial values are load-bearing: swallowing them loses each
            # signal's starting value, which makes its first real transition
            # invisible -- and that transition is a toggle the count needs.
            if kw in ("dumpvars", "dumpon"):
                initialising = True
                continue

            if kw == "dumpoff":
                # Everything goes to x; forget the previous values so the first
                # post-`$dumpon` value is not counted as a toggle.
                initialising = False
                last.clear()
                continue

            args: list[str] = []
            while i < n:
                tok = read_token()
                if tok == "" and i >= n:
                    break
                if tok == "$end":
                    break
                args.append(tok)
            if kw == "timescale":
                joined = " ".join(args)
                m = _TS_RE.match(joined)
                if m:
                    act.timescale_s = _TS_FACTOR[m.group(2)] * float(m.group(1))
            elif kw == "scope":
                if len(args) >= 2:
                    scopes.append(args[1])
            elif kw == "upscope":
                if scopes:
                    scopes.pop()
            elif kw == "var":
                # $var <type> <width> <id> <ref> [<index>] $end
                if len(args) >= 4:
                    vtype, width_s, vid, ref = args[0], args[1], args[2], args[3]
                    try:
                        width = int(width_s)
                    except ValueError:
                        width = 1
                    extra = args[4] if len(args) > 4 else ""
                    name = f"{ref}{extra}" if extra.startswith("[") else ref
                    sig = VcdSignal(
                        id=vid, name=name, width=width, vcd_type=vtype, scope=".".join(scopes)
                    )
                    ids[vid] = sig
                    act.signals[vid] = sig
                    act.stats[vid] = ToggleStat(name=sig.full_name, width=width)
            elif kw == "enddefinitions":
                in_header = False
            continue

        if ch == "#":
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            try:
                time_units = int(text[i + 1 : j])
            except ValueError:
                pass
            i = j
            continue

        if in_header:
            # Unknown directive outside a header; skip the line.
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue

        # ---- value change ---------------------------------------------------
        if ch in "01xXzZ":
            vid = text[i + 1 : i + 2]
            val = ch.lower()
            i += 2
        elif ch in "bBrR":
            j = i + 1
            while j < n and text[j] not in " \t\r\n":
                j += 1
            val = text[i + 1 : j].lower()
            k = j
            while k < n and text[k] in " \t\r\n":
                k += 1
            vid = text[k : k + 1]
            i = k + 1 if vid else k
            if ch in "rR":
                val = _real_to_bits(val)
        else:
            # Unrecognised token: skip a line.
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue

        st = act.stats.get(vid)
        if st is None:
            continue
        prev = last.get(vid)
        if initialising:
            # Initial condition from $dumpvars: record it as the baseline so the
            # first real transition can be counted, but do not count it.
            last[vid] = val
            if val.count("1"):
                st.high_time_units += 1
            continue
        if prev is not None and _popcount_diff(prev, val):
            flips = _popcount_diff(prev, val)
            changes += 1
            st.value_changes += 1
            st.bit_toggles += flips
        if val.count("1"):
            st.high_time_units += 1
        last[vid] = val
        if max_changes is not None and changes >= max_changes:
            break

    act.end_time_units = time_units
    return act


def _real_to_bits(text: str) -> str:
    """VCD ``r`` real value -> a bit pattern is meaningless; return zeros."""
    return "0"


def parse_vcd_file(path: str | os.PathLike[str], max_changes: int | None = None) -> VcdActivity:
    """Read *path* (UTF-8) and parse it."""
    p = os.fspath(path)
    if not os.path.exists(p):
        raise VcdParseError(f"VCD file not found: {p}")
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        return parse_vcd(fh.read(), source=p, max_changes=max_changes)


# ---------------------------------------------------------------------------
# Relative power model
# ---------------------------------------------------------------------------

#: Default capacitance/swing weight per net class. These are *unitless stand-ins*
#: for C*V^2, chosen so the relative comparison reflects the well-known fact that
#: a clock network costs far more per toggle than ordinary data. They are not
#: extracted from any library and must not be read as physical capacitance.
DEFAULT_WEIGHTS: dict[str, float] = {
    "clock": 4.0,
    "reset": 1.5,
    "control": 1.0,
    "data": 1.0,
}


@dataclass
class PowerModel:
    """Weights and classification rules for the relative power estimate."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    clock_patterns: tuple[str, ...] = ("clk", "clock")
    reset_patterns: tuple[str, ...] = ("rst", "reset", "nrst")
    weight_by_width: bool = False
    width_exponent: float = 1.0

    def classify(self, name: str) -> str:
        low = name.lower()
        base = low.rsplit(".", 1)[-1]
        for p in self.clock_patterns:
            if p in base or p in low:
                return "clock"
        for p in self.reset_patterns:
            if p in base or p in low:
                return "reset"
        return "data"

    def weight(self, name: str) -> float:
        return float(self.weights.get(self.classify(name), 1.0))


@dataclass
class PowerEstimate:
    """Result of applying the model to one VCD."""

    label: str = ""
    relative_power: float = 0.0
    by_class: dict[str, float] = field(default_factory=dict)
    toggling_nets: int = 0
    total_bit_toggles: int = 0
    duration_ns: float = 0.0
    model_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "relative_power": self.relative_power,
            "by_class": self.by_class,
            "toggling_nets": self.toggling_nets,
            "total_bit_toggles": self.total_bit_toggles,
            "duration_ns": self.duration_ns,
            "model": self.model_note,
        }


MODEL_NOTE = (
    "P_rel = SUM_nets( class_weight(net) * bit_toggle_rate(net) ), unitless and "
    "normalised against the reference run. No process library, no parasitics, no "
    "clock-tree model, no leakage: this is a relative comparison only and is NOT "
    "signoff power analysis."
)


def estimate(
    activity: VcdActivity,
    model: PowerModel | None = None,
    label: str = "",
    duration_ns: float | None = None,
) -> PowerEstimate:
    """Apply the relative switching-power model to a parsed VCD.

    Nets that never toggle contribute zero, exactly as the physics says.
    """
    m = model or PowerModel()
    dur = duration_ns if duration_ns is not None else activity.duration_ns
    by_class: dict[str, float] = {}
    total = 0.0
    toggling = 0

    for st in activity.stats.values():
        if st.bit_toggles == 0:
            continue
        toggling += 1
        rate = st.toggle_rate_hz(dur)
        cls = m.classify(st.name)
        w = m.weight(st.name)
        contribution = w * rate
        if m.weight_by_width:
            contribution *= float(st.width) ** m.width_exponent
        by_class[cls] = by_class.get(cls, 0.0) + contribution
        total += contribution

    return PowerEstimate(
        label=label,
        relative_power=total,
        by_class=dict(sorted(by_class.items())),
        toggling_nets=toggling,
        total_bit_toggles=activity.total_bit_toggles,
        duration_ns=dur,
        model_note=MODEL_NOTE,
    )


def compare(reference: PowerEstimate, candidate: PowerEstimate) -> dict[str, Any]:
    """Compare two estimates: absolute ratio and per-class ratios."""
    ref = reference.relative_power
    cand = candidate.relative_power
    ratios: dict[str, float] = {}
    for cls in sorted(set(reference.by_class) | set(candidate.by_class)):
        r = reference.by_class.get(cls, 0.0)
        c = candidate.by_class.get(cls, 0.0)
        ratios[cls] = (c / r) if r else 0.0
    delta_pct = ((cand - ref) / ref * 100.0) if ref else 0.0
    return {
        "reference": reference.label,
        "candidate": candidate.label,
        "reference_relative_power": ref,
        "candidate_relative_power": cand,
        "ratio": (cand / ref) if ref else 0.0,
        "delta_pct": delta_pct,
        "class_ratios": ratios,
        "note": MODEL_NOTE,
    }
