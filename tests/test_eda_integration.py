"""Integration tests that need the real EDA toolchain.

Everything here is skipped automatically when yosys / nextpnr-ecp5 / iverilog are
not reachable, which is the case on the CI runners (see README "Tests and CI":
CI verifies the parsing and analysis layers, the full flow needs a local OSS CAD
Suite).

These tests are deliberately small: they run the self-checking RTL simulations
and one synthesis, not the whole five-variant flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tclab import flow as F
from tclab import runner as R

from .conftest import EDA_AVAILABLE, requires_eda

pytestmark = requires_eda


# ---------------------------------------------------------------------------
# RTL self-check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "top,pipe_stages,latency",
    [("fir_top_chain", 0, 1), ("fir_top_tree", 0, 1), ("fir_top_pipe", 2, 3)],
)
def test_rtl_variants_pass_their_self_checking_testbench(top, pipe_stages, latency, tmp_path):
    """The RTL is checked against a behavioural golden model, not just compiled."""
    r = F.run_simulation(top=top, pipe_stages=pipe_stages, outdir=tmp_path)
    assert r["ok"], r
    assert r["errors"] == 0
    assert r["checks"] > 500
    assert r["latency"] == latency


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def test_synthesis_produces_a_json_netlist(tmp_path):
    out = tmp_path / "netlist.json"
    F.synth(str(out), chparams={"SUM_STYLE": 0, "PIPE_STAGES": 0})
    assert out.exists()
    assert out.stat().st_size > 1000


def _ff_count_from_json(path) -> int:
    """Count flip-flop primitives in a synthesised JSON netlist.

    Read from the netlist rather than from the log: yosys's stat output format
    differs between versions (the older package in a distro archive prints a
    different layout), while the JSON cell types are stable.
    """
    import json

    netlist = json.loads(Path(path).read_text(encoding="utf-8"))
    count = 0
    for module in netlist.get("modules", {}).values():
        for cell in module.get("cells", {}).values():
            if str(cell.get("type", "")).endswith("TRELLIS_FF"):
                count += 1
    return count


def test_pipelined_variant_synthesises_more_flops_than_baseline(tmp_path):
    """The pipeline really does add registers -- a cheap sanity check on chparam.

    SYMPTOM this guards: chparam silently not applying, so every variant would
    synthesise the same baseline and the timing comparison would be meaningless.
    """
    a = tmp_path / "base.json"
    b = tmp_path / "piped.json"
    F.synth(str(a), chparams={"SUM_STYLE": 0, "PIPE_STAGES": 0})
    F.synth(str(b), chparams={"SUM_STYLE": 1, "PIPE_STAGES": 2})
    base = _ff_count_from_json(a)
    piped = _ff_count_from_json(b)
    assert base > 0, "no flip-flops found in the baseline netlist"
    assert piped > base, f"expected the retimed build to add flops ({piped} vs {base})"


def test_synthesis_is_deterministic(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    F.synth(str(a), chparams={"SUM_STYLE": 1, "PIPE_STAGES": 0})
    F.synth(str(b), chparams={"SUM_STYLE": 1, "PIPE_STAGES": 0})
    assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------
# Place and route
# ---------------------------------------------------------------------------


def test_place_and_route_reads_the_sdc_and_reports(tmp_path):
    """The SDC file must actually be consumed by nextpnr.

    nextpnr echoes ``Info: constraining clock net 'clk' to <freq> MHz`` when it
    has read a create_clock. That line is the evidence the constraint took
    effect, and it is what this test asserts on.

    Skipped when the installed nextpnr predates ``--sdc`` support: the package in
    Ubuntu's archive has no such option, so there is no way to apply constraints
    there. That is a real toolchain limitation, not something to paper over --
    ``tclab.flow`` detects it and omits the flag with a warning.
    """
    from tclab.runner import tool_supports

    if not tool_supports("nextpnr-ecp5", "--sdc"):
        pytest.skip("installed nextpnr-ecp5 does not support --sdc")

    js = tmp_path / "n.json"
    rep = tmp_path / "r.json"
    log = tmp_path / "pnr.log"
    F.synth(str(js), chparams={"SUM_STYLE": 0, "PIPE_STAGES": 0})
    F.place_and_route(
        str(js),
        str(rep),
        sdc_in="constr/fir_block_nextpnr.sdc",
        freq_mhz=100.0,
        log=str(log),
    )
    assert rep.exists()
    text = log.read_text(encoding="utf-8")
    assert "constraining clock net 'clk'" in text
    assert "100.00 MHz" in text


def test_place_and_route_output_parses_and_analyses(tmp_path):
    from tclab import report_parse as RP
    from tclab import sta as S

    js = tmp_path / "n.json"
    rep = tmp_path / "r.json"
    F.synth(str(js), chparams={"SUM_STYLE": 0, "PIPE_STAGES": 0})
    F.place_and_route(str(js), str(rep), freq_mhz=100.0)
    report = RP.parse_report_file(str(rep))
    assert not report.is_empty()
    assert report.utilization.dsp > 0
    summary = S.analyze(report)
    assert summary.primary is not None
    assert summary.primary.achieved_mhz > 0
    # The critical path must reproduce the tool's own Fmax.
    assert summary.critical_path["implied_fmax_mhz"] == pytest.approx(
        summary.primary.achieved_mhz, rel=1e-6
    )


def test_pipelining_improves_fmax_over_the_baseline(tmp_path):
    """The headline claim, measured end to end rather than assumed."""
    from tclab import report_parse as RP
    from tclab import sta as S

    def fmax(style: int, stages: int, name: str) -> float:
        js = tmp_path / f"{name}.json"
        rep = tmp_path / f"{name}_r.json"
        F.synth(str(js), chparams={"SUM_STYLE": style, "PIPE_STAGES": stages})
        F.place_and_route(str(js), str(rep), freq_mhz=100.0)
        return S.analyze(RP.parse_report_file(str(rep))).primary.achieved_mhz

    base = fmax(0, 0, "base")
    piped = fmax(1, 2, "pipe")
    assert piped > base, f"pipelining should raise Fmax ({piped} vs {base})"


# ---------------------------------------------------------------------------
# LEC
# ---------------------------------------------------------------------------


def test_pipeline_equivalence_proof_succeeds():
    """The retimed build must be proven equivalent modulo its added latency."""
    out = F.run_lec("flow/lec_pipe_equiv.ys", timeout=300)
    assert out["status"] == "proven", out
