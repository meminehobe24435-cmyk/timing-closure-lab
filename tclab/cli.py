"""tclab.cli -- command line entry point.

    py -3.12 -m tclab run          # full flow: synth -> P&R -> STA -> reports
    py -3.12 -m tclab sta <report> # analyse one existing nextpnr report
    py -3.12 -m tclab sdc <file>   # parse and dump an SDC constraint file
    py -3.12 -m tclab power        # VCD toggle rates + relative power comparison
    py -3.12 -m tclab lec          # logical equivalence checks
    py -3.12 -m tclab sim          # self-checking RTL simulation
    py -3.12 -m tclab env          # report the resolved toolchain and versions
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__, flow, lec as lec_mod, power as power_mod, report_parse, sdc as sdc_mod, sta as sta_mod
from .runner import ToolError, ToolUnavailable, Toolchain, repo_root, tool_versions

__all__ = ["main", "build_parser"]


def _p(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_env(args: argparse.Namespace) -> int:
    tc = Toolchain.resolve()
    _p(f"tclab {__version__}")
    _p(f"repo root : {repo_root()}")
    _p(f"wrapper   : {tc.describe()}")
    _p("versions:")
    for k, v in tool_versions(tc).items():
        _p(f"  {k:<16} {v}")
    return 0


def cmd_sdc(args: argparse.Namespace) -> int:
    try:
        d = sdc_mod.parse_file(args.file, strict=not args.lenient)
    except sdc_mod.SdcError as exc:
        _p(f"SDC ERROR: {exc}")
        return 2
    if args.json:
        _p(json.dumps(_sdc_to_dict(d), indent=2, ensure_ascii=False))
        return 0
    _p(f"file     : {d.filename}")
    _p("summary  :")
    for k, v in d.summary().items():
        _p(f"  {k:<24} {v}")
    for c in d.clocks:
        _p(f"clock    : {c.name} period={c.period_ns} ns -> {c.frequency_mhz:.3f} MHz"
           f" target={c.target}")
    for g in d.generated_clocks:
        _p(f"genclock : {g.name} divide_by={g.divide_by} multiply_by={g.multiply_by}"
           f" -> period={g.period_ns(d.clocks)} ns")
    for cg in d.clock_groups:
        _p(f"groups   : {cg.mode} {cg.groups}")
    _p(f"primary period: {d.primary_period_ns} ns")
    return 0


def _sdc_to_dict(d: sdc_mod.SdcDesign) -> dict[str, Any]:
    from dataclasses import asdict

    return {
        "filename": d.filename,
        "summary": d.summary(),
        "clocks": [asdict(c) for c in d.clocks],
        "generated_clocks": [asdict(g) for g in d.generated_clocks],
        "input_delays": [asdict(x) for x in d.input_delays],
        "output_delays": [asdict(x) for x in d.output_delays],
        "max_delays": [asdict(x) for x in d.max_delays],
        "false_paths": [asdict(x) for x in d.false_paths],
        "multicycle_paths": [asdict(x) for x in d.multicycle_paths],
        "clock_groups": [asdict(x) for x in d.clock_groups],
        "primary_period_ns": d.primary_period_ns,
    }


def cmd_sta(args: argparse.Namespace) -> int:
    try:
        rep = report_parse.parse_report_file(args.report)
    except report_parse.ReportError as exc:
        _p(f"REPORT ERROR: {exc}")
        return 2
    s = sta_mod.analyze(rep, top_n=args.top)
    if args.json:
        _p(json.dumps(s.as_dict(), indent=2, ensure_ascii=False))
        return 0
    _p(f"report: {args.report}")
    if rep.is_empty():
        _p("  (report contains no timing or utilisation data)")
    u = rep.utilization
    _p(f"resources: LUT4={u.lut4} FF={u.ff} DSP={u.dsp} BRAM={u.bram} IO={u.io}")
    for c in s.clocks:
        _p(f"clock {c.clock}: constraint={c.constraint_mhz:.3f} MHz "
           f"achieved={c.achieved_mhz:.3f} MHz slack={c.setup_slack_ns:+.3f} ns "
           f"{'MET' if c.met else 'VIOLATED'}")
    if s.critical_path:
        cp = s.critical_path
        _p(f"critical path: {cp['start_point']} -> {cp['end_point']} "
           f"{cp['total_ns']:.3f} ns ({cp['steps']} steps)")
        bt = cp.get("by_type", {})
        _p(f"  logic={bt.get('logic_ns', 0):.3f} ns routing={bt.get('routing_ns', 0):.3f} ns "
           f"clk-to-q={bt.get('clk_to_q_ns', 0):.3f} ns")
    _p("top bottlenecks by capture point:")
    for b in s.bottlenecks_by_endpoint:
        _p(f"  {b.worst_arrival_ns:8.3f} ns  {b.name}")
    _p("conclusions:")
    for c in s.conclusions:
        _p(f"  - {c}")
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    model = power_mod.PowerModel()
    ests: list[power_mod.PowerEstimate] = []
    for vcd in args.vcd:
        act = power_mod.parse_vcd_file(vcd)
        label = Path(vcd).stem
        e = power_mod.estimate(act, model, label=label)
        ests.append(e)
        _p(f"{label}: nets_toggling={e.toggling_nets} bit_toggles={e.total_bit_toggles} "
           f"duration={e.duration_ns:.1f} ns P_rel={e.relative_power:.6e}")
        for cls, val in e.by_class.items():
            _p(f"    {cls:<8} {val:.6e}")
    if len(ests) >= 2:
        cmp_ = power_mod.compare(ests[0], ests[1])
        _p("")
        _p(f"ratio {cmp_['candidate']} / {cmp_['reference']} = {cmp_['ratio']:.6f} "
           f"({cmp_['delta_pct']:+.2f}%)")
        for cls, r in cmp_["class_ratios"].items():
            _p(f"    {cls:<8} x{r:.6f}")
        _p(cmp_["note"])
    if args.json:
        _p(json.dumps([e.as_dict() for e in ests], indent=2))
    return 0


def cmd_lec(args: argparse.Namespace) -> int:
    results = lec_mod.run_all(logdir=args.logdir)
    for r in results:
        _p(f"{r.check.name}: {r.status.upper()} ({r.duration_s:.1f}s) - {r.detail}")
        _p(f"    claim    : {r.check.claim}")
        _p(f"    criterion: {r.check.criterion}")
    s = lec_mod.summary(results)
    _p(f"\nproven {s['proven_count']}/{s['total']} checks")
    if args.json:
        Path(args.json).write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    cases = [
        ("fir_top_chain", 0),
        ("fir_top_tree", 0),
        ("fir_top_pipe", 2),
    ]
    if args.top:
        cases = [(args.top, args.pipe_stages)]
    rc = 0
    out: list[dict[str, Any]] = []
    for top, ps in cases:
        r = flow.run_simulation(top=top, pipe_stages=ps, outdir=args.outdir)
        out.append(r)
        mark = "PASS" if r.get("ok") else "FAIL"
        _p(f"{top} (PIPE={ps}, latency={r.get('latency')}): {mark} "
           f"checks={r.get('checks')} errors={r.get('errors')}")
        if not r.get("ok"):
            rc = 1
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return rc


def cmd_run(args: argparse.Namespace) -> int:
    outdir = args.out
    tc = Toolchain.resolve()
    _p(f"tclab {__version__} -- timing-closure flow")
    _p(f"wrapper: {tc.describe()}")
    _p(f"output : {outdir}")

    variants = flow.DEFAULT_VARIANTS
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        variants = [v for v in variants if v.name in wanted]
        if not variants:
            _p(f"no variant matched --only {args.only!r}")
            return 2
    if args.freq:
        variants = [
            flow.Variant(**{**v.__dict__, "freq_mhz": args.freq}) for v in variants
        ]

    _p("\n[1/5] synthesis + place & route")
    results = flow.run_all(variants, outdir=outdir, chain=tc, progress=_p)

    _p("\n[2/5] RTL self-checking simulation")
    sims = []
    for top, ps in (("fir_top_chain", 0), ("fir_top_tree", 0), ("fir_top_pipe", 2)):
        r = flow.run_simulation(top=top, pipe_stages=ps, outdir="sim_out", chain=tc)
        sims.append(r)
        _p(f"  {top}: {'PASS' if r.get('ok') else 'FAIL'} checks={r.get('checks')} "
           f"errors={r.get('errors')}")

    _p("\n[3/5] VCD activity + relative power")
    power_summary: dict[str, Any] = {}
    try:
        vcds = _run_activity(tc)
        model = power_mod.PowerModel()
        ests = []
        for path in vcds:
            act = power_mod.parse_vcd_file(path)
            e = power_mod.estimate(act, model, label=Path(path).stem)
            ests.append(e)
            _p(f"  {e.label}: nets={e.toggling_nets} bit_toggles={e.total_bit_toggles} "
               f"P_rel={e.relative_power:.6e}")
        power_summary = {
            "estimates": [e.as_dict() for e in ests],
            "comparisons": [
                power_mod.compare(ests[0], e) for e in ests[1:]
            ] if len(ests) > 1 else [],
        }
        for c in power_summary["comparisons"]:
            _p(f"  {c['candidate']} vs {c['reference']}: x{c['ratio']:.4f} "
               f"({c['delta_pct']:+.2f}%)")
    except (ToolError, ToolUnavailable, power_mod.VcdParseError) as exc:
        _p(f"  power study skipped: {exc}")
        power_summary = {"error": str(exc)}

    _p("\n[4/5] logical equivalence checks")
    lec_results = lec_mod.run_all(logdir=f"{outdir}/lec", chain=tc)
    for r in lec_results:
        _p(f"  {r.check.name}: {r.status.upper()} - {r.detail}")
    lec_summary = lec_mod.summary(lec_results)

    _p("\n[5/5] writing reports")
    paths = flow.write_reports(
        results,
        outdir=outdir,
        extra={
            "toolchain": {"wrapper": tc.describe(), "versions": tool_versions(tc)},
            "simulation": sims,
            "power": power_summary,
            "lec": lec_summary,
            "sdc": _sdc_to_dict(sdc_mod.parse_file("constr/fir_block.sdc")),
        },
    )
    _p(f"  wrote {paths['json']}")
    _p(f"  wrote {paths['markdown']}")

    failed = [r.name for r in results if not r.ok]
    if failed:
        _p(f"\nvariants that failed to build: {', '.join(failed)}")
        return 1
    return 0


def _run_activity(tc: Toolchain) -> list[str]:
    """Compile and run the activity testbench for each power scenario."""
    from .runner import run_tool

    out = Path("sim_out")
    out.mkdir(parents=True, exist_ok=True)
    vvp = out / "tb_activity.vvp"
    run_tool(
        "iverilog",
        [
            "-g2012", "-I", "rtl", "-D", "TOP_MODULE=fir_block_top",
            "-o", str(vvp),
            "rtl/fir_sum.v", "rtl/fir_core.v", "rtl/fir_block_top.v", "sim/tb_activity.v",
        ],
        chain=tc,
    )
    paths: list[str] = []
    for mode in ("full", "gated", "idle"):
        vcd = f"sim_out/activity_{mode}.vcd"
        run_tool(
            "vvp",
            [str(vvp), f"+vcd={vcd}", f"+mode={mode}", "+cycles=3000", "+stride=4"],
            chain=tc,
            check=False,
        )
        if Path(vcd).exists():
            paths.append(vcd)
    return paths


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tclab",
        description="timing-closure lab: yosys -> nextpnr-ecp5 -> STA, with SDC parsing, "
        "equivalence checking and a VCD-based relative power study.",
    )
    p.add_argument("--version", action="version", version=f"tclab {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("env", help="show the resolved toolchain and tool versions")
    sp.set_defaults(func=cmd_env)

    sp = sub.add_parser("run", help="run the whole flow and write reports")
    sp.add_argument("--out", default="reports", help="output directory (default: reports)")
    sp.add_argument("--only", default="", help="comma-separated subset of variant names")
    sp.add_argument("--freq", type=float, default=None, help="override the target frequency in MHz")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("sta", help="analyse an existing nextpnr JSON report")
    sp.add_argument("report")
    sp.add_argument("--top", type=int, default=10, help="entries per bottleneck list")
    sp.add_argument("--json", action="store_true", help="emit JSON")
    sp.set_defaults(func=cmd_sta)

    sp = sub.add_parser("sdc", help="parse and dump an SDC constraint file")
    sp.add_argument("file")
    sp.add_argument("--lenient", action="store_true", help="ignore unknown commands")
    sp.add_argument("--json", action="store_true", help="emit JSON")
    sp.set_defaults(func=cmd_sdc)

    sp = sub.add_parser("power", help="VCD toggle rates and relative power comparison")
    sp.add_argument("vcd", nargs="+", help="one or more VCD files (first is the reference)")
    sp.add_argument("--json", action="store_true", help="emit JSON after the summary")
    sp.set_defaults(func=cmd_power)

    sp = sub.add_parser("lec", help="run the logical equivalence checks")
    sp.add_argument("--logdir", default="reports/lec")
    sp.add_argument("--json", default="", help="also write the summary JSON here")
    sp.set_defaults(func=cmd_lec)

    sp = sub.add_parser("sim", help="run the self-checking RTL simulations")
    sp.add_argument("--top", default="", help="single top module instead of all three")
    sp.add_argument("--pipe-stages", type=int, default=0, dest="pipe_stages")
    sp.add_argument("--outdir", default="sim_out")
    sp.add_argument("--json", default="")
    sp.set_defaults(func=cmd_sim)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _p("interrupted")
        return 130
    except (ToolError, ToolUnavailable) as exc:
        _p(f"TOOL ERROR: {exc}")
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
