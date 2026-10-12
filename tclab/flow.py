"""tclab.flow -- end-to-end orchestration of the timing-closure flow.

One variant of the experiment is:

    yosys synth_ecp5  ->  nextpnr-ecp5 (place & route + STA)  ->  parse -> analyse

and one *run* is a list of variants (baseline, re-associated, pipelined, plus
control runs with timing-driven placement off) executed back to back so their
numbers are directly comparable.

Everything is driven through :mod:`tclab.runner`, so the same code works with
``tools/oss.bat`` on Windows and ``tools/oss.sh`` in CI.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import report_parse, sdc as sdc_mod, sta as sta_mod
from .report_parse import NextpnrReport
from .runner import ToolError, Toolchain, ToolUnavailable, run_tool
from .sta import StaSummary

__all__ = [
    "Variant",
    "VariantResult",
    "FlowError",
    "DEFAULT_VARIANTS",
    "synth",
    "place_and_route",
    "run_variant",
    "run_all",
    "experiment_table",
    "write_reports",
    "run_simulation",
    "run_lec",
]

#: ECP5 part used throughout. LFE5U-45F / CABGA381 / speed grade 6.
DEVICE_ARGS = ["--45k", "--package", "CABGA381", "--speed", "6"]

DEFAULT_FREQ_MHZ = 100.0


class FlowError(Exception):
    """A step of the flow failed in a way the caller must know about."""


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    """One configuration of the design to build and measure."""

    name: str
    sum_style: int = 0
    pipe_stages: int = 0
    freq_mhz: float = DEFAULT_FREQ_MHZ
    no_tmdriv: bool = False
    seed: int = 1
    description: str = ""
    latency: int = 1

    @property
    def chparams(self) -> dict[str, int]:
        return {"SUM_STYLE": self.sum_style, "PIPE_STAGES": self.pipe_stages}


#: The experiment matrix actually run by ``tclab run``.
#:
#:   baseline      serial accumulation chain, no pipeline (worst case)
#:   tree          balanced adder tree, same latency (re-association only)
#:   tree_pipe1    adder tree + 1 register cut      (+1 clock latency)
#:   pipe          adder tree + 2 register cuts     (+2 clocks latency)
#:   pipe_notmdriv identical to `pipe` but with timing-driven placement OFF,
#:                 which isolates how much of the gain is placement vs logic
DEFAULT_VARIANTS: list[Variant] = [
    Variant(
        name="baseline",
        sum_style=0,
        pipe_stages=0,
        latency=1,
        description="serial accumulation chain (15 adders in series), timing-critical baseline",
    ),
    Variant(
        name="tree",
        sum_style=1,
        pipe_stages=0,
        latency=1,
        description="balanced adder tree, zero added latency (re-association only)",
    ),
    Variant(
        name="tree_pipe1",
        sum_style=1,
        pipe_stages=1,
        latency=2,
        description="adder tree with one register cut (+1 clock)",
    ),
    Variant(
        name="pipe",
        sum_style=1,
        pipe_stages=2,
        latency=3,
        description="adder tree with two register cuts (+2 clocks)",
    ),
    Variant(
        name="pipe_notmdriv",
        sum_style=1,
        pipe_stages=2,
        no_tmdriv=True,
        latency=3,
        description="identical to `pipe` but timing-driven placement disabled (control)",
    ),
]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class VariantResult:
    """Everything produced by building and measuring one variant."""

    variant: Variant
    ok: bool = False
    error: str = ""
    json_path: str = ""
    report_path: str = ""
    log_path: str = ""
    report: NextpnrReport | None = None
    sta: StaSummary | None = None

    @property
    def name(self) -> str:
        return self.variant.name

    def metrics(self) -> dict[str, Any]:
        """The flat number set used for the comparison table and the JSON."""
        m: dict[str, Any] = {
            "variant": self.variant.name,
            "sum_style": self.variant.sum_style,
            "pipe_stages": self.variant.pipe_stages,
            "latency_clocks": self.variant.latency,
            "freq_constraint_mhz": self.variant.freq_mhz,
            "timing_driven_placement": not self.variant.no_tmdriv,
            "ok": self.ok,
        }
        if not self.ok or self.report is None or self.sta is None:
            m["error"] = self.error
            return m
        u = self.report.utilization
        m["lut4"] = u.lut4
        m["ff"] = u.ff
        m["bram"] = u.bram
        m["dsp"] = u.dsp
        m["io"] = u.io
        prim = self.sta.primary
        if prim is not None:
            m["fmax_mhz"] = prim.achieved_mhz
            m["worst_path_ns"] = prim.worst_path_ns
            m["setup_slack_ns"] = prim.setup_slack_ns
            m["timing_met"] = prim.met
        cp = self.sta.critical_path or {}
        m["critical_path_start"] = cp.get("start_point", "")
        m["critical_path_end"] = cp.get("end_point", "")
        m["critical_path_steps"] = cp.get("steps", 0)
        split = self.sta.delay_split
        m["logic_ns"] = split.get("logic_ns", 0.0)
        m["routing_ns"] = split.get("routing_ns", 0.0)
        m["clk_to_q_ns"] = split.get("clk_to_q_ns", 0.0)
        return m

    def as_dict(self) -> dict[str, Any]:
        return {
            "variant": asdict(self.variant),
            "ok": self.ok,
            "error": self.error,
            "paths": {
                "json": self.json_path,
                "report": self.report_path,
                "log": self.log_path,
            },
            "metrics": self.metrics(),
            "sta": self.sta.as_dict() if self.sta else None,
        }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def synth(
    out_json: str | os.PathLike[str],
    chparams: Mapping[str, int] | None = None,
    top: str = "fir_block_top",
    rtl: Sequence[str] = ("rtl/fir_sum.v", "rtl/fir_core.v", "rtl/fir_block_top.v"),
    log: str | os.PathLike[str] | None = None,
    chain: Toolchain | None = None,
    timeout: float = 1800.0,
):
    """Run ``yosys`` + ``synth_ecp5`` and write a JSON netlist for nextpnr.

    ``rtl`` lists only top-level source files: ``rtl/fir_coeffs.vh`` is pulled in
    by ``include`` from the files that need it, and naming it here as well makes
    yosys read it twice and fail on redefined localparams.

    yosys separates commands with ``;``, so the script is assembled from whole
    commands rather than from a flat list of tokens -- joining the tokens with
    spaces makes yosys read the entire line as one command.
    """
    script = ["read_verilog -I rtl " + " ".join(rtl)]
    hier = ["hierarchy", "-top", top]
    for k, v in (chparams or {}).items():
        hier += ["-chparam", str(k), str(v)]
    script.append(" ".join(hier))
    script.append(f"synth_ecp5 -top {top} -json {out_json}")
    return run_tool(
        "yosys",
        ["-Q", "-T", "-p", "; ".join(script)],
        log_path=log,
        chain=chain,
        timeout=timeout,
    )


def place_and_route(
    json_in: str | os.PathLike[str],
    report_out: str | os.PathLike[str],
    sdc_in: str | os.PathLike[str] | None = None,
    freq_mhz: float | None = None,
    no_tmdriv: bool = False,
    seed: int = 1,
    textcfg_out: str | os.PathLike[str] | None = None,
    log: str | os.PathLike[str] | None = None,
    chain: Toolchain | None = None,
    timeout: float = 3600.0,
    allow_timing_fail: bool = True,
    require_sdc: bool = False,
) -> Any:
    """Run ``nextpnr-ecp5`` with SDC constraints and detailed timing reporting.

    ``--sdc`` is passed only if the installed nextpnr actually advertises it.
    The OSS CAD Suite build used here (nextpnr-0.10-82) does; the older package
    in a distro archive may not, and passing an unknown option there is a hard
    error rather than a warning. Set ``require_sdc=True`` to make the absence an
    error instead of a degradation.
    """
    args = list(DEVICE_ARGS)
    args += ["--json", str(json_in)]
    args += ["--lpf-allow-unconstrained"]
    sdc_used = False
    if sdc_in is not None:
        if _sdc_supported(chain):
            args += ["--sdc", str(sdc_in)]
            sdc_used = True
        elif require_sdc:
            raise FlowError(
                "this nextpnr-ecp5 build does not support --sdc, so the timing "
                f"constraints in {sdc_in} cannot be applied"
            )
        else:
            log_hint = f" (see {log})" if log else ""
            warnings.warn(
                f"nextpnr-ecp5 does not support --sdc; running without the "
                f"constraints in {sdc_in}{log_hint}",
                RuntimeWarning,
                stacklevel=2,
            )
    if freq_mhz is not None:
        args += ["--freq", f"{freq_mhz:g}"]
    if no_tmdriv:
        args += ["--no-tmdriv"]
    args += ["--seed", str(seed)]
    args += ["--report", str(report_out)]
    args += ["--detailed-timing-report"]
    if textcfg_out is not None:
        args += ["--textcfg", str(textcfg_out)]
    if allow_timing_fail:
        # Without this nextpnr exits non-zero whenever the design misses its
        # target frequency, which is exactly the situation this lab measures.
        args += ["--timing-allow-fail"]
    result = run_tool("nextpnr-ecp5", args, log_path=log, chain=chain, timeout=timeout)
    try:
        setattr(result, "sdc_applied", sdc_used)
    except AttributeError:  # pragma: no cover - ToolResult is a plain dataclass
        pass
    return result


def _sdc_supported(chain: Toolchain | None) -> bool:
    from .runner import tool_supports

    return tool_supports("nextpnr-ecp5", "--sdc", chain=chain)


def run_variant(
    variant: Variant,
    outdir: str | os.PathLike[str] = "reports",
    sdc_in: str | os.PathLike[str] = "constr/fir_block_nextpnr.sdc",
    top: str = "fir_block_top",
    chain: Toolchain | None = None,
) -> VariantResult:
    """Build one variant end to end and analyse its timing report."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    res = VariantResult(
        variant=variant,
        json_path=str(out / f"{variant.name}.json"),
        report_path=str(out / f"{variant.name}_report.json"),
        log_path=str(out / f"{variant.name}.log"),
    )
    try:
        synth(
            res.json_path,
            chparams=variant.chparams,
            top=top,
            log=str(out / f"{variant.name}_synth.log"),
            chain=chain,
        )
        place_and_route(
            res.json_path,
            res.report_path,
            sdc_in=sdc_in,
            freq_mhz=variant.freq_mhz,
            no_tmdriv=variant.no_tmdriv,
            seed=variant.seed,
            textcfg_out=str(out / f"{variant.name}.config"),
            log=res.log_path,
            chain=chain,
        )
    except (ToolError, ToolUnavailable) as exc:
        res.ok = False
        res.error = str(exc)
        return res

    res.report = report_parse.parse_report_file(res.report_path)
    res.sta = sta_mod.analyze(res.report)
    res.ok = True
    return res


def run_all(
    variants: Iterable[Variant] | None = None,
    outdir: str | os.PathLike[str] = "reports",
    sdc_in: str | os.PathLike[str] = "constr/fir_block_nextpnr.sdc",
    chain: Toolchain | None = None,
    progress=None,
) -> list[VariantResult]:
    """Run every variant in order, returning one result each."""
    vs = list(variants) if variants is not None else list(DEFAULT_VARIANTS)
    results: list[VariantResult] = []
    for v in vs:
        if progress:
            progress(f"building variant {v.name} ...")
        r = run_variant(v, outdir=outdir, sdc_in=sdc_in, chain=chain)
        results.append(r)
        if progress:
            if r.ok and r.sta and r.sta.primary:
                progress(
                    f"  {v.name}: Fmax={r.sta.primary.achieved_mhz:.3f} MHz "
                    f"({'MET' if r.sta.primary.met else 'FAIL'})"
                )
            else:
                progress(f"  {v.name}: FAILED - {r.error.splitlines()[0] if r.error else '?'}")
    return results


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def experiment_table(results: Sequence[VariantResult]) -> dict[str, Any]:
    """Build the optimise-before/optimise-after comparison from real numbers.

    Nothing here is estimated: every figure comes from a nextpnr run that
    actually happened. Variants that failed are reported as failed, and the
    baseline/optimised pair is only populated when both runs succeeded.
    """
    by_name = {r.variant.name: r for r in results}
    rows = [r.metrics() for r in results]

    def fmax_of(name: str) -> float | None:
        r = by_name.get(name)
        if r is None or not r.ok or r.sta is None or r.sta.primary is None:
            return None
        return r.sta.primary.achieved_mhz

    def path_of(name: str) -> dict[str, Any] | None:
        r = by_name.get(name)
        if r is None or not r.ok or r.sta is None:
            return None
        return r.sta.critical_path

    baseline_fmax = fmax_of("baseline")
    best = None
    best_fmax = None
    for r in results:
        if not r.ok or r.sta is None or r.sta.primary is None:
            continue
        if best_fmax is None or r.sta.primary.achieved_mhz > best_fmax:
            best_fmax = r.sta.primary.achieved_mhz
            best = r.variant.name

    improvements: list[dict[str, Any]] = []
    if baseline_fmax:
        for r in results:
            if not r.ok or r.sta is None or r.sta.primary is None:
                continue
            if r.variant.name == "baseline":
                continue
            f = r.sta.primary.achieved_mhz
            improvements.append(
                {
                    "variant": r.variant.name,
                    "description": r.variant.description,
                    "fmax_before_mhz": baseline_fmax,
                    "fmax_after_mhz": f,
                    "delta_mhz": f - baseline_fmax,
                    "improvement_pct": 100.0 * (f - baseline_fmax) / baseline_fmax,
                    "latency_clocks": r.variant.latency,
                    "lut4_delta": r.report.utilization.lut4 - by_name["baseline"].report.utilization.lut4
                    if by_name.get("baseline") and by_name["baseline"].ok
                    else None,
                }
            )

    ctrl = None
    pipe = by_name.get("pipe")
    notm = by_name.get("pipe_notmdriv")
    if (
        pipe is not None and pipe.ok and pipe.sta and pipe.sta.primary
        and notm is not None and notm.ok and notm.sta and notm.sta.primary
    ):
        ctrl = {
            "with_timing_driven_placement_mhz": pipe.sta.primary.achieved_mhz,
            "without_timing_driven_placement_mhz": notm.sta.primary.achieved_mhz,
            "delta_mhz": pipe.sta.primary.achieved_mhz - notm.sta.primary.achieved_mhz,
            "delta_pct": 100.0
            * (pipe.sta.primary.achieved_mhz - notm.sta.primary.achieved_mhz)
            / notm.sta.primary.achieved_mhz
            if notm.sta.primary.achieved_mhz
            else None,
        }

    return {
        "rows": rows,
        "baseline_fmax_mhz": baseline_fmax,
        "best_variant": best,
        "best_fmax_mhz": best_fmax,
        "overall_improvement_pct": (
            100.0 * (best_fmax - baseline_fmax) / baseline_fmax
            if baseline_fmax and best_fmax
            else None
        ),
        "improvements": improvements,
        "timing_driven_placement_control": ctrl,
        "baseline_critical_path": path_of("baseline"),
        "optimised_critical_path": path_of(best) if best else None,
        "note": (
            "All figures are nextpnr-ecp5 results for LFE5U-45F/CABGA381/speed-6 "
            "produced by this run. No figure is estimated or carried over."
        ),
    }


# ---------------------------------------------------------------------------
# Simulation and LEC helpers
# ---------------------------------------------------------------------------


def run_simulation(
    top: str = "fir_top_chain",
    pipe_stages: int = 0,
    outdir: str | os.PathLike[str] = "sim_out",
    tb: str = "sim/tb_fir.v",
    rtl: Sequence[str] = ("rtl/fir_sum.v", "rtl/fir_core.v", "rtl/fir_top.v"),
    chain: Toolchain | None = None,
) -> dict[str, Any]:
    """Compile and run a self-checking testbench; return its verdict."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    vvp = out / f"{top}.vvp"
    try:
        run_tool(
            "iverilog",
            [
                "-g2012", "-I", "rtl",
                "-D", f"TOP_MODULE={top}",
                "-D", f"PIPE_STAGES={pipe_stages}",
                "-o", str(vvp),
                *rtl, tb,
            ],
            chain=chain,
        )
        r = run_tool("vvp", [str(vvp)], chain=chain, check=False)
    except (ToolError, ToolUnavailable) as exc:
        return {"top": top, "ok": False, "error": str(exc)}

    text = r.output
    passed = "TB_PASS" in text
    checks = 0
    errors = 0
    m = re.search(r"checks=(\d+) errors=(\d+)", text)
    if m:
        checks, errors = int(m.group(1)), int(m.group(2))
    return {
        "top": top,
        "pipe_stages": pipe_stages,
        "latency": 1 + pipe_stages,
        "ok": passed and r.returncode == 0,
        "checks": checks,
        "errors": errors,
        "verdict": "TB_PASS" if passed else "TB_FAIL",
        "log_tail": "\n".join(text.strip().splitlines()[-3:]),
    }


def run_lec(
    script: str,
    log: str | os.PathLike[str] | None = None,
    chain: Toolchain | None = None,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """Run a yosys LEC script and classify the outcome.

    The classification is deliberately literal: it reports what yosys actually
    said, including "did not converge", rather than asserting a proof that was
    not obtained.
    """
    try:
        r = run_tool("yosys", ["-Q", "-T", script], log_path=log, chain=chain,
                     check=False, timeout=timeout)
    except ToolUnavailable as exc:
        return {"script": script, "status": "unavailable", "detail": str(exc)}

    text = r.output
    status = "unknown"
    detail = ""
    if "SAT proof finished - no model found" in text or "SAT proof finished - UNSAT" in text:
        status = "proven"
        detail = "SAT reported no counterexample"
    elif "Equivalence successfully proven" in text:
        status = "proven"
        detail = "equiv_status reported all cells proven"
    elif "model found: FAIL" in text or "proof did fail" in text:
        status = "counterexample"
        detail = "the solver found a counterexample"
    elif "unproven $equiv cells" in text:
        m = re.search(r"Found a total of (\d+) unproven", text)
        status = "unproven"
        detail = f"{m.group(1)} $equiv cells remained unproven" if m else "unproven cells remain"
    elif r.returncode == -9 or "TIMEOUT" in text:
        status = "timeout"
        detail = f"did not converge within {timeout:g}s"
    elif r.returncode != 0:
        status = "error"
        detail = "\n".join(text.strip().splitlines()[-3:])

    return {
        "script": script,
        "status": status,
        "detail": detail,
        "returncode": r.returncode,
        "duration_s": round(r.duration_s, 2),
        "log": str(log) if log else None,
        "excerpt": "\n".join(
            ln for ln in text.splitlines()
            if any(k in ln for k in ("SAT proof", "unproven $equiv", "Equivalence", "Solving problem"))
        )[:4000],
    }


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------


def write_reports(
    results: Sequence[VariantResult],
    outdir: str | os.PathLike[str] = "reports",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Write ``timing_summary.json`` and ``sta_report.md`` from real results."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    table = experiment_table(results)
    payload: dict[str, Any] = {
        "variants": [r.as_dict() for r in results],
        "experiment": table,
    }
    if extra:
        payload.update(extra)
    json_path = out / "timing_summary.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    md_path = out / "sta_report.md"
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(results, table, extra))
    return {"json": str(json_path), "markdown": str(md_path)}


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(
    results: Sequence[VariantResult],
    table: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Render the STA report. Every number is read from a real run."""
    L: list[str] = []
    L.append("# STA / timing-closure report\n")
    L.append(
        "Generated by `tclab` from nextpnr-ecp5 reports. Target device "
        "LFE5U-45F, package CABGA381, speed grade 6. Every number below is read "
        "out of a report produced by an actual place-and-route run.\n"
    )

    L.append("## Achieved Fmax and resources\n")
    L.append(
        "| variant | SUM_STYLE | PIPE | latency | LUT4 | FF | DSP | BRAM | Fmax (MHz) "
        "| worst path (ns) | setup slack (ns) | met |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        m = r.metrics()
        if not r.ok:
            L.append(
                f"| {r.variant.name} | {r.variant.sum_style} | {r.variant.pipe_stages} "
                f"| {r.variant.latency} | - | - | - | - | FAILED | - | - | - |"
            )
            continue
        L.append(
            f"| {r.variant.name} | {m['sum_style']} | {m['pipe_stages']} | {m['latency_clocks']} "
            f"| {m['lut4']} | {m['ff']} | {m['dsp']} | {m['bram']} "
            f"| {_fmt(m.get('fmax_mhz'))} | {_fmt(m.get('worst_path_ns'))} "
            f"| {_fmt(m.get('setup_slack_ns'))} | {'YES' if m.get('timing_met') else 'NO'} |"
        )
    L.append("")

    b = table.get("baseline_fmax_mhz")
    best = table.get("best_fmax_mhz")
    if b and best:
        L.append(
            f"**Baseline {_fmt(b)} MHz -> best {_fmt(best)} MHz "
            f"({_fmt(table.get('overall_improvement_pct'), 1)}% improvement), "
            f"best variant `{table.get('best_variant')}`.**\n"
        )

    imps = table.get("improvements") or []
    if imps:
        L.append("## Optimisation steps (before -> after)\n")
        L.append("| step | change | latency | Fmax before (MHz) | Fmax after (MHz) | delta (MHz) | delta (%) |")
        L.append("|---|---|---|---|---|---|---|")
        for i in imps:
            L.append(
                f"| {i['variant']} | {i['description']} | +{i['latency_clocks'] - 1} clk "
                f"| {_fmt(i['fmax_before_mhz'])} | {_fmt(i['fmax_after_mhz'])} "
                f"| {_fmt(i['delta_mhz'])} | {_fmt(i['improvement_pct'], 1)} |"
            )
        L.append("")

    ctrl = table.get("timing_driven_placement_control")
    if ctrl:
        L.append("## Control: timing-driven placement\n")
        L.append(
            f"- timing-driven placement ON : {_fmt(ctrl['with_timing_driven_placement_mhz'])} MHz\n"
            f"- timing-driven placement OFF: {_fmt(ctrl['without_timing_driven_placement_mhz'])} MHz\n"
            f"- measured effect: {_fmt(ctrl['delta_mhz'])} MHz "
            f"({_fmt(ctrl['delta_pct'], 1)}%)\n"
        )

    for label, key in (("Baseline critical path", "baseline_critical_path"),
                       ("Best-variant critical path", "optimised_critical_path")):
        cp = table.get(key)
        if not cp:
            continue
        L.append(f"## {label}\n")
        L.append(
            f"- start point: `{cp.get('start_point')}`\n"
            f"- end point: `{cp.get('end_point')}`\n"
            f"- total delay: {_fmt(cp.get('total_ns'))} ns over {cp.get('steps')} steps\n"
            f"- implied Fmax: {_fmt(cp.get('implied_fmax_mhz'))} MHz\n"
        )
        bt = cp.get("by_type") or {}
        L.append(
            f"- split: logic {_fmt(bt.get('logic_ns'))} ns, routing {_fmt(bt.get('routing_ns'))} ns, "
            f"clk-to-q {_fmt(bt.get('clk_to_q_ns'))} ns, setup {_fmt(bt.get('setup_ns'))} ns\n"
        )
        culprits = cp.get("culprit_nets") or []
        if culprits:
            L.append("- slowest nets on the path:\n")
            for c in culprits[:5]:
                L.append(f"  - `{c['net']}` {_fmt(c['delay_ns'])} ns\n")
        L.append("")

    L.append("## Conclusions\n")
    for r in results:
        if r.ok and r.sta:
            L.append(f"### {r.variant.name}\n")
            for c in r.sta.conclusions:
                L.append(f"- {c}")
            L.append(f"- hold: {r.sta.hold_note}")
            L.append("")

    if extra:
        for k, v in extra.items():
            if k.startswith("_"):
                continue
    return "\n".join(L) + "\n"
