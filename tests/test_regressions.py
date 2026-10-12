"""Regression tests for problems that were actually hit while building this lab.

Each test names the symptom that was observed and pins the fix so it cannot
silently come back. These are the "inject a known problem, assert it is caught"
tests: several of them assert that a guard exists at all, because the guard is
the fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tclab import flow as F
from tclab import runner as R
from tests.test_runner import _LAST_POPEN, _fake_popen

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Testbench race: driving din *at* the clock edge
# ---------------------------------------------------------------------------
# SYMPTOM: 674 of 720 checks failed with values that were individually plausible,
#          and the DUT and the testbench's reference model drifted apart.
# CAUSE:   din was assigned in the same time step as the sampling clock edge, so
#          the DUT's shift register and the testbench's history register could
#          latch different values.
# FIX:     every stimulus change happens 1 ns AFTER a rising edge.


def test_tb_fir_never_drives_stimulus_at_a_rising_edge():
    text = (ROOT / "sim" / "tb_fir.v").read_text(encoding="utf-8")
    # Strip comments so prose about the bug is not mistaken for the bug.
    code = "\n".join(ln.split("//")[0] for ln in text.splitlines())

    # No bare assignment to din/rst_n/checking immediately after a posedge wait:
    # they must be preceded by a #1 delay.
    bad = re.findall(r"@\(posedge clk\)\s*;\s*\n\s*(?:din|rst_n|checking)\s*=", code)
    assert not bad, f"stimulus assigned at a clock edge: {bad}"

    # And the race-free pattern must actually be present.
    assert "#1 din" in code
    assert "#1 rst_n" in code


def test_tb_fir_has_a_watchdog():
    text = (ROOT / "sim" / "tb_fir.v").read_text(encoding="utf-8")
    assert "TB_TIMEOUT" in text, "a hung testbench must fail rather than run forever"


def test_tb_fir_has_a_clock_generator():
    """SYMPTOM: the simulation ran to its watchdog with checks=0.

    CAUSE: the clock generator was dropped during an edit; nothing ever ticked.
    FIX: an explicit always-block clock, asserted here.
    """
    text = (ROOT / "sim" / "tb_fir.v").read_text(encoding="utf-8")
    assert re.search(r"always\s+#5\s+clk\s*=\s*~clk", text)


def test_tb_fir_enables_checking_after_the_reference_fills():
    """SYMPTOM: checks=0 -- the harness ran but compared nothing.

    CAUSE: `checking` was initialised to 0 and never set to 1.
    FIX: checking is enabled only after the reference history has filled.
    """
    text = (ROOT / "sim" / "tb_fir.v").read_text(encoding="utf-8")
    assert "checking = 1'b1" in text
    assert "LAT + 16" in text


# ---------------------------------------------------------------------------
# 2. yosys command assembly
# ---------------------------------------------------------------------------
# SYMPTOM: every synthesis run died in 0.1 s with
#          "ERROR: Command syntax error: Found option, expected arguments."
# CAUSE:   the yosys script was assembled by joining tokens with spaces, so the
#          whole line was parsed as one command.
# FIX:     commands are joined with "; ".


def test_synth_builds_a_semicolon_separated_script(monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

    def fake_run(tool, args=(), **kwargs):
        captured["tool"] = tool
        captured["args"] = list(args)
        return FakeCompleted()

    monkeypatch.setattr(F, "run_tool", fake_run)
    F.synth("reports/x.json", chparams={"SUM_STYLE": 1}, chain=R.Toolchain(wrapper=None, prefix=[]))

    script = captured["args"][captured["args"].index("-p") + 1]
    assert "read_verilog" in script
    assert "hierarchy" in script
    assert "synth_ecp5" in script
    # The three commands must be separated, not space-joined into one.
    assert script.count(";") >= 2


def test_synth_passes_chparams_through(monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

    def fake_run(tool, args=(), **kwargs):
        captured["args"] = list(args)
        return FakeCompleted()

    monkeypatch.setattr(F, "run_tool", fake_run)
    F.synth(
        "reports/x.json",
        chparams={"SUM_STYLE": 1, "PIPE_STAGES": 2},
        chain=R.Toolchain(wrapper=None, prefix=[]),
    )
    script = captured["args"][captured["args"].index("-p") + 1]
    assert "-chparam SUM_STYLE 1" in script
    assert "-chparam PIPE_STAGES 2" in script


def test_synth_does_not_list_the_include_file_explicitly(monkeypatch):
    """SYMPTOM: "ERROR: Re-definition of module" / duplicate localparams.

    CAUSE: rtl/fir_coeffs.vh was passed to read_verilog *and* pulled in by
    `include from the files that need it.
    FIX: only top-level sources are listed; the header comes in via `include.
    """
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

    def fake_run(tool, args=(), **kwargs):
        captured["args"] = list(args)
        return FakeCompleted()

    monkeypatch.setattr(F, "run_tool", fake_run)
    F.synth("reports/x.json", chain=R.Toolchain(wrapper=None, prefix=[]))
    script = captured["args"][captured["args"].index("-p") + 1]
    assert "fir_coeffs.vh" not in script
    assert "fir_core.v" in script


# ---------------------------------------------------------------------------
# 3. nextpnr must not abort on a timing failure
# ---------------------------------------------------------------------------
# SYMPTOM: nextpnr exited 1 whenever the design missed its target frequency,
#          which is exactly the case this lab is measuring.
# FIX:     --timing-allow-fail is passed unless the caller opts out.


def test_place_and_route_passes_timing_allow_fail(monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

    def fake_run(tool, args=(), **kwargs):
        captured["tool"] = tool
        captured["args"] = list(args)
        return FakeCompleted()

    monkeypatch.setattr(F, "run_tool", fake_run)
    F.place_and_route("a.json", "b.json", chain=R.Toolchain(wrapper=None, prefix=[]))
    assert captured["tool"] == "nextpnr-ecp5"
    assert "--timing-allow-fail" in captured["args"]


def test_place_and_route_requests_a_detailed_timing_report(monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

    def fake_run(tool, args=(), **kwargs):
        captured["args"] = list(args)
        return FakeCompleted()

    monkeypatch.setattr(F, "run_tool", fake_run)
    F.place_and_route("a.json", "b.json", chain=R.Toolchain(wrapper=None, prefix=[]))
    assert "--detailed-timing-report" in captured["args"]
    assert "--report" in captured["args"]


def test_place_and_route_passes_no_tmdriv_only_when_requested(monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

    def fake_run(tool, args=(), **kwargs):
        captured["args"] = list(args)
        return FakeCompleted()

    monkeypatch.setattr(F, "run_tool", fake_run)
    F.place_and_route("a.json", "b.json", no_tmdriv=False, chain=R.Toolchain(wrapper=None, prefix=[]))
    assert "--no-tmdriv" not in captured["args"]

    F.place_and_route("a.json", "b.json", no_tmdriv=True, chain=R.Toolchain(wrapper=None, prefix=[]))
    assert "--no-tmdriv" in captured["args"]


# ---------------------------------------------------------------------------
# 4. SDC support is not what the vendor documentation implies
# ---------------------------------------------------------------------------
# SYMPTOM: "ERROR: Unsupported SDC command 'create_generated_clock'".
# CAUSE:   nextpnr-ecp5 0.10-82 implements only create_clock.
# FIX:     ship two SDC files -- the full constraint set, and the subset nextpnr
#          actually consumes -- and say so in the README.


def test_nextpnr_sdc_contains_only_commands_nextpnr_accepts():
    text = (ROOT / "constr" / "fir_block_nextpnr.sdc").read_text(encoding="utf-8")
    code = "\n".join(ln.split("#")[0] for ln in text.splitlines())
    commands = re.findall(r"^\s*([a-z_]+)\s", code, flags=re.MULTILINE)
    assert commands, "the nextpnr SDC subset must contain at least one command"
    assert set(commands) == {"create_clock"}, f"unexpected commands: {set(commands)}"


def test_full_sdc_keeps_every_required_command():
    text = (ROOT / "constr" / "fir_block.sdc").read_text(encoding="utf-8")
    for cmd in (
        "create_clock",
        "create_generated_clock",
        "set_input_delay",
        "set_output_delay",
        "set_max_delay",
        "set_false_path",
        "set_multicycle_path",
        "set_clock_groups",
    ):
        assert re.search(rf"^\s*{cmd}\s", text, flags=re.MULTILINE), f"{cmd} missing"


# ---------------------------------------------------------------------------
# 5. The LEC bounded proof needs the reset state pinned
# ---------------------------------------------------------------------------
# SYMPTOM: "SAT proof finished - model found: FAIL!" on a claim that is true.
# CAUSE:   `sat -seq N` starts from an unconstrained FF state, not the reset
#          state, so the delay line and the pipeline registers were unrelated.
# FIX:     -set-init-zero pins the initial state to the design's reset values.


def test_pipe_lec_script_pins_the_initial_state():
    text = (ROOT / "flow" / "lec_pipe_equiv.ys").read_text(encoding="utf-8")
    sat_lines = [ln for ln in text.splitlines() if ln.strip().startswith("sat ")]
    assert sat_lines, "the LEC script must run a sat proof"
    assert any("-set-init-zero" in ln for ln in sat_lines), (
        "without -set-init-zero the bounded proof starts from an arbitrary state "
        "and reports a spurious counterexample"
    )


def test_pipe_lec_script_converts_async_reset():
    """SAT has no model for async FFs; async2sync must run first."""
    text = (ROOT / "flow" / "lec_pipe_equiv.ys").read_text(encoding="utf-8")
    assert "async2sync" in text


def test_pipe_lec_harness_delays_the_reference_by_the_added_latency():
    """The criterion is a shifted output sequence, not same-cycle equality."""
    text = (ROOT / "rtl" / "lec_pipe_wrapper.v").read_text(encoding="utf-8")
    assert "LATENCY_SHIFT" in text
    assert "d2 <= d1" in text, "the reference output must be delayed"
    assert "fir_top_tree u_base" in text
    assert "fir_top_pipe u_pipe" in text


# ---------------------------------------------------------------------------
# 6. The tree summation is hard-wired for 16 taps
# ---------------------------------------------------------------------------
# SYMPTOM: a scaled-down LEC run reported a counterexample instead of proving.
# CAUSE:   fir_sum_tree indexes prod[2*g+1] up to prod[15] regardless of
#          N_TAPS, so instantiating it with N_TAPS=8 reads out of range. The
#          "counterexample" was a real behavioural difference caused by the
#          invalid configuration, not a saturation of the solver.
# FIX:     the 16-tap constraint is stated in the RTL and in the README, and the
#          scaling probe no longer changes N_TAPS.


def test_fir_sum_documents_the_sixteen_tap_constraint():
    text = (ROOT / "rtl" / "fir_sum.v").read_text(encoding="utf-8")
    assert "16" in text
    low = text.lower()
    assert "16 taps" in low or "sixteen" in low or "n_taps" in low


def test_scaling_probe_does_not_change_tap_count():
    p = ROOT / "flow" / "lec_sum_equiv_scaling_probe.ys"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "N_TAPS" not in text, (
        "changing N_TAPS makes fir_sum_tree read out of range; the probe must "
        "scale width only"
    )


# ---------------------------------------------------------------------------
# 7. Non-ASCII repository path
# ---------------------------------------------------------------------------
# SYMPTOM: yosys reported that a file plainly present on disk did not exist.
# CAUSE:   cmd.exe re-encoded the command line, mangling the CJK directory name.
# FIX:     never put such a path on the command line; relativise first.


def test_run_tool_relativises_and_then_guards(monkeypatch):
    captured = {}

    monkeypatch.setattr(R.subprocess, "Popen", _fake_popen())
    root = R.repo_root()
    R.run_tool(
        "yosys",
        ["-p", "script", "-o", str(root / "reports" / "y.json")],
        cwd=root,
        chain=R.Toolchain(wrapper=None, prefix=[]),
    )
    assert not any("工程" in str(a) for a in _LAST_POPEN[-1].argv)


# ---------------------------------------------------------------------------
# 8. PowerShell writes a BOM; TCL and yosys do not accept one
# ---------------------------------------------------------------------------
# SYMPTOM: "TCL interpreter returned an error: invalid command name '\ufeffputs'".
# CAUSE:   Set-Content -Encoding utf8 on Windows PowerShell emits a UTF-8 BOM.
# FIX:     TCL and Verilog sources are written without a BOM.


@pytest.mark.parametrize(
    "rel",
    [
        "flow/lec_pipe_equiv.ys",
        "flow/lec_sum_equiv.ys",
        "flow/lec_sum_equiv_scaling_probe.ys",
        "rtl/fir_sum.v",
        "rtl/fir_core.v",
        "rtl/fir_top.v",
        "tclab/sdc.py",
        "constr/fir_block.sdc",
    ],
)
def test_source_files_have_no_utf8_bom(rel):
    raw = (ROOT / rel).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} starts with a UTF-8 BOM"


@pytest.mark.parametrize(
    "rel",
    ["rtl/fir_sum.v", "rtl/fir_core.v", "rtl/fir_top.v", "rtl/fir_block_top.v", "sim/tb_fir.v"],
)
def test_rtl_files_are_utf8_decodable(rel):
    (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 9. Analysis must not be flattered by a short extracted path
# ---------------------------------------------------------------------------


def test_fmax_uses_the_worst_of_tool_and_extracted_path():
    """SYMPTOM: none yet -- this is a deliberate guard.

    The tool's `achieved` and the extracted critical path are two independent
    measurements of the same quantity. Taking the larger path delay means a
    truncated report can only make the design look *worse*, never better.
    """
    from tclab import report_parse as RP
    from tclab import sta as S

    rep = RP.parse_report(
        {
            "fmax": {"clk": {"achieved": 200.0, "constraint": 100.0}},
            "critical_paths": [
                {
                    "from": "posedge clk",
                    "to": "posedge clk",
                    "path": [{"type": "logic", "delay": 25.0, "from": {"cell": "a"}, "to": {"cell": "b"}}],
                }
            ],
        }
    )
    s = S.analyze(rep)
    assert s.primary.achieved_mhz == 40.0
    assert not s.primary.met


# ---------------------------------------------------------------------------
# 11. `"" in "abc"` is True -- an empty peek must not drive a skip loop
# ---------------------------------------------------------------------------
# SYMPTOM: the whole test suite hung. Individually the tests were fine; the
#          session never finished and produced no output past "collected".
#          Bisected down to a single `sdc.parse(...)` call.
# CAUSE:   `_WS` was a *string*, and membership against a string is a substring
#          test. At end of input peek() returns "", and `"" in " \t\r"` is True,
#          so the whitespace-skip loop in parse_command spun forever calling a
#          no-op advance().
#          The shipped SDC file ends with a newline, so the CLI path worked and
#          only the in-memory unit tests (no trailing newline) hung.
# FIX:     _WS/_BARE_STOP are frozensets, and the skip loop is guarded by
#          at_end().


def test_parse_without_trailing_newline_terminates():
    """The exact input shape that used to hang: no trailing newline."""
    from tclab import sdc as S

    d = S.parse("create_clock -name c -period 10 [get_ports clk]")
    assert d.clocks[0].period_ns == 10.0


def test_parse_with_trailing_newline_still_works():
    from tclab import sdc as S

    d = S.parse("create_clock -name c -period 10 [get_ports clk]\n")
    assert d.clocks[0].period_ns == 10.0


@pytest.mark.parametrize("text", ["", "   ", "\t\t", "\n\n\n", "  ;  ", "# just a comment"])
def test_degenerate_sdc_inputs_terminate(text):
    from tclab import sdc as S

    d = S.parse(text)
    assert d.clocks == []


def test_whitespace_constants_are_sets_not_strings():
    """Guards the root cause directly: a string here reintroduces the hang."""
    from tclab import sdc as S

    assert isinstance(S._WS, (set, frozenset))
    assert isinstance(S._BARE_STOP, (set, frozenset))
    assert "" not in S._WS
    assert "" not in S._BARE_STOP


# ---------------------------------------------------------------------------
# 10. The comparison table must not invent numbers for failed runs
# ---------------------------------------------------------------------------


def test_failed_variant_is_excluded_from_improvement_maths():
    from tclab import flow as FL

    ok = FL.VariantResult(
        variant=FL.Variant(name="baseline"),
        ok=True,
        report=__import__("tclab.report_parse", fromlist=["x"]).parse_report(
            {
                "fmax": {"clk": {"achieved": 50.0, "constraint": 100.0}},
                "critical_paths": [
                    {
                        "from": "posedge clk",
                        "to": "posedge clk",
                        "path": [{"type": "logic", "delay": 20.0, "from": {"cell": "a"}, "to": {"cell": "b"}}],
                    }
                ],
            }
        ),
    )
    from tclab import sta as ST

    ok.sta = ST.analyze(ok.report)
    broken = FL.VariantResult(variant=FL.Variant(name="broken"), ok=False, error="boom")

    t = FL.experiment_table([ok, broken])
    assert t["best_variant"] == "baseline"
    assert all(i["variant"] != "broken" for i in t["improvements"])
