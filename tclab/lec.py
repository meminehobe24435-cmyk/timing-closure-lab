"""tclab.lec -- logical equivalence checking (LEC) driver.

Three checks are run, and it matters that they are described correctly, because
they establish different things:

1. ``flow/lec_pipe_equiv.ys`` -- **retiming equivalence, formally proven.**
   ``fir_top_tree`` and ``fir_top_pipe`` contain the *same* balanced adder tree;
   the only difference is that two register stages were inserted into it. The
   claim proven is

       fir_top_pipe.dout(t) == fir_top_tree.dout(t - 2)   for all t

   That "shifted by two clocks" criterion is the whole point: a pipeline does not
   produce the same value at the same time, it produces the same *sequence*
   later. The harness delays the reference output by exactly the added latency
   and asserts equality every cycle from reset.

2. ``flow/lec_sum_equiv.ys`` -- **re-association (chain vs balanced tree).**
   This asks whether ``sum(p0..p15)`` computed as a serial chain equals the same
   sum computed as a balanced tree. It is a combinational equivalence question.

   Its result is reported honestly: bit-level CNF-SAT equivalence of two
   *differently structured* adder networks is a known hardness result for
   resolution, and it does not converge at full width here (measured, see the
   README). The tool reports whatever actually happened; nothing is upgraded to
   "proven" unless yosys said so.

3. ``flow/lec_sum_equiv_small.ys`` -- a scaling probe for the same obligation at
   reduced width. It is *not* run by default, because the measurement it produced
   is that the wall is not a width effect that can be scaled around: the
   chain-vs-tree miter failed to converge at W=37 (>10 min), W=12 (>400 s) and
   W=6 (>45 s). The script is kept in the tree so the measurement is reproducible;
   the README records the numbers.

The full-width functional guarantee for the re-association is carried instead by
the 720-cycle self-checking simulation in ``sim/tb_fir.v``, which checks every
variant against a behavioural golden model at its declared latency. Chain and
tree are therefore compared transitively, over 720 vectors, with zero mismatches.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .runner import Toolchain
from .flow import run_lec as _run_script

__all__ = [
    "LecCheck",
    "LecResult",
    "DEFAULT_CHECKS",
    "run_check",
    "run_all",
    "LATENCY_CRITERION",
]

LATENCY_CRITERION = (
    "Pipelined equivalence is defined as output-sequence equality modulo the added "
    "latency: fir_top_pipe.dout(t) == fir_top_tree.dout(t - LATENCY_SHIFT), with "
    "LATENCY_SHIFT = PIPE_STAGES = 2. Equality at the same t is deliberately NOT "
    "claimed, because it is false for any pipeline."
)


@dataclass
class LecCheck:
    """One equivalence-check script plus the claim it is supposed to establish."""

    name: str
    script: str
    claim: str
    criterion: str
    scope: str = "formal"
    timeout_s: float = 900.0


@dataclass
class LecResult:
    check: LecCheck
    status: str
    detail: str = ""
    duration_s: float = 0.0
    log: str = ""
    excerpt: str = ""

    @property
    def proven(self) -> bool:
        return self.status == "proven"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.check.name,
            "script": self.check.script,
            "claim": self.check.claim,
            "criterion": self.check.criterion,
            "scope": self.check.scope,
            "status": self.status,
            "proven": self.proven,
            "detail": self.detail,
            "duration_s": self.duration_s,
            "log": self.log,
            "excerpt": self.excerpt,
        }


DEFAULT_CHECKS: list[LecCheck] = [
    LecCheck(
        name="retiming_pipeline_vs_reference",
        script="flow/lec_pipe_equiv.ys",
        claim="the pipelined implementation is functionally equivalent to the un-pipelined "
        "adder tree, delayed by the two clocks the pipeline added",
        criterion=LATENCY_CRITERION,
        scope="formal (sequential)",
    ),
    LecCheck(
        name="reassociation_chain_vs_tree_full_width",
        script="flow/lec_sum_equiv.ys",
        claim="the balanced adder tree computes the same sum as the serial accumulation chain, "
        "at full width (37-bit accumulator, 16 taps)",
        criterion="combinational equivalence, no latency shift",
        scope="formal (combinational, full width) - known to be SAT-hard; expected not to converge",
        timeout_s=60.0,
    ),
]


def run_check(
    check: LecCheck,
    logdir: str | os.PathLike[str] = "reports/lec",
    chain: Toolchain | None = None,
) -> LecResult:
    """Run one equivalence script and classify what yosys actually reported."""
    out = Path(logdir)
    out.mkdir(parents=True, exist_ok=True)
    log = out / f"{check.name}.log"
    raw = _run_script(check.script, log=str(log), chain=chain, timeout=check.timeout_s)
    return LecResult(
        check=check,
        status=raw.get("status", "unknown"),
        detail=raw.get("detail", ""),
        duration_s=float(raw.get("duration_s", 0.0)),
        log=str(log),
        excerpt=raw.get("excerpt", ""),
    )


def run_all(
    checks: Iterable[LecCheck] | None = None,
    logdir: str | os.PathLike[str] = "reports/lec",
    chain: Toolchain | None = None,
) -> list[LecResult]:
    """Run every equivalence check."""
    cs = list(checks) if checks is not None else list(DEFAULT_CHECKS)
    return [run_check(c, logdir=logdir, chain=chain) for c in cs]


def summary(results: Sequence[LecResult]) -> dict[str, Any]:
    """A one-glance verdict block for ``timing_summary.json`` and the README."""
    return {
        "criterion": LATENCY_CRITERION,
        "checks": [r.as_dict() for r in results],
        "proven_count": sum(1 for r in results if r.proven),
        "total": len(results),
        "note": (
            "A check is only reported as proven when yosys reported a completed proof. "
            "Checks that did not converge are reported with their measured status."
        ),
    }
