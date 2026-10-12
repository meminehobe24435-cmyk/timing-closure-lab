"""Tests for tclab.flow -- orchestration, comparison arithmetic and reporting.

The comparison logic is exercised with synthetic VariantResults so the arithmetic
(before/after deltas, the timing-driven-placement control, the "best variant"
selection) can be pinned down exactly. Real end-to-end runs are covered
separately in test_eda_integration.py.
"""

from __future__ import annotations

import json

import pytest

from tclab import flow as F
from tclab import report_parse as R
from tclab import runner as RR
from tclab import sta as S

from .conftest import requires_eda


def _report(achieved=100.0, constraint=100.0, lut4=100, ff=50, dsp=16, path_ns=None):
    if path_ns is None:
        path_ns = 1000.0 / achieved
    return R.parse_report(
        {
            "fmax": {"clk": {"achieved": achieved, "constraint": constraint}},
            "utilization": {
                "TRELLIS_COMB": {"used": lut4, "available": 43848},
                "TRELLIS_FF": {"used": ff, "available": 43848},
                "MULT18X18D": {"used": dsp, "available": 144},
                "DP16KD": {"used": 0, "available": 56},
                "TRELLIS_IO": {"used": 93, "available": 197},
            },
            "critical_paths": [
                {
                    "from": "posedge clk",
                    "to": "posedge clk",
                    "path": [
                        {"type": "clk-to-q", "delay": 0.5, "from": {"cell": "u.sr[0]_TRELLIS_FF_Q_1"}, "to": {"cell": "u.sr[0]_TRELLIS_FF_Q_1"}},
                        {"type": "logic", "delay": path_ns * 0.4, "from": {"cell": "a"}, "to": {"cell": "b"}},
                        {"type": "routing", "delay": path_ns * 0.6 - 0.5, "from": {"cell": "b"}, "to": {"cell": "c"}},
                        {"type": "setup", "delay": 0.0, "from": {"cell": "c"}, "to": {"cell": "c"}},
                    ],
                }
            ],
        }
    )


def _result(name, achieved, constraint=100.0, **kw):
    v = F.Variant(name=name, freq_mhz=constraint, **{k: v for k, v in kw.items() if k in ("sum_style", "pipe_stages", "no_tmdriv", "latency")})
    rep = _report(achieved=achieved, constraint=constraint, **{k: v for k, v in kw.items() if k in ("lut4", "ff", "dsp")})
    return F.VariantResult(variant=v, ok=True, report=rep, sta=S.analyze(rep))


# ---------------------------------------------------------------------------
# Variant model
# ---------------------------------------------------------------------------


def test_variant_chparams():
    v = F.Variant(name="x", sum_style=1, pipe_stages=2)
    assert v.chparams == {"SUM_STYLE": 1, "PIPE_STAGES": 2}


def test_default_variants_cover_the_required_controls():
    names = {v.name for v in F.DEFAULT_VARIANTS}
    assert "baseline" in names
    assert "pipe" in names
    assert "pipe_notmdriv" in names
    # there must be at least one variant with timing-driven placement disabled
    assert any(v.no_tmdriv for v in F.DEFAULT_VARIANTS)
    assert any(not v.no_tmdriv for v in F.DEFAULT_VARIANTS)


def test_default_variants_have_matching_latency():
    for v in F.DEFAULT_VARIANTS:
        if v.sum_style == 1:
            assert v.latency == 1 + v.pipe_stages


def test_device_args_target_ecp5_45f():
    assert "--45k" in F.DEVICE_ARGS
    assert "CABGA381" in F.DEVICE_ARGS


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_flatten_the_expected_numbers():
    r = _result("baseline", 50.0)
    m = r.metrics()
    assert m["variant"] == "baseline"
    assert m["fmax_mhz"] == pytest.approx(50.0)
    assert m["lut4"] == 100
    assert m["dsp"] == 16
    assert m["timing_met"] is False
    assert m["critical_path_start"]


def test_metrics_of_failed_variant_reports_error():
    v = F.Variant(name="broken")
    r = F.VariantResult(variant=v, ok=False, error="yosys exploded")
    m = r.metrics()
    assert m["ok"] is False
    assert m["error"] == "yosys exploded"
    assert "fmax_mhz" not in m


def test_place_and_route_omits_sdc_when_the_tool_lacks_it(monkeypatch):
    """Version drift: the nextpnr in a distro archive may have no --sdc.

    SYMPTOM: CI failed on Ubuntu with "unrecognised option '--sdc'". The tool
    there is older than the OSS CAD Suite build this lab targets, so the flag
    must be probed rather than assumed.
    """

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

        @property
        def output(self):
            return self.stdout + ("\n" + self.stderr if self.stderr else "")

    captured = {}

    def fake_run(tool, args=(), **kwargs):
        captured["args"] = list(args)
        return _P()

    monkeypatch.setattr(F, "run_tool", fake_run)
    monkeypatch.setattr(F, "_sdc_supported", lambda chain: False)

    with pytest.warns(RuntimeWarning):
        F.place_and_route(
            "a.json",
            "b.json",
            sdc_in="constr/fir_block_nextpnr.sdc",
            chain=RR.Toolchain(wrapper=None, prefix=[]),
        )
    assert "--sdc" not in captured["args"]


def test_place_and_route_can_require_sdc(monkeypatch):
    class _P:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

        @property
        def output(self):
            return ""

    monkeypatch.setattr(F, "run_tool", lambda *a, **k: _P())
    monkeypatch.setattr(F, "_sdc_supported", lambda chain: False)
    with pytest.raises(F.FlowError):
        F.place_and_route(
            "a.json",
            "b.json",
            sdc_in="constr/fir_block_nextpnr.sdc",
            require_sdc=True,
            chain=RR.Toolchain(wrapper=None, prefix=[]),
        )


def test_place_and_route_includes_sdc_when_supported(monkeypatch):
    class _P:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 0.0

        @property
        def output(self):
            return ""

    captured = {}

    def fake_run(tool, args=(), **kwargs):
        captured["args"] = list(args)
        return _P()

    monkeypatch.setattr(F, "run_tool", fake_run)
    monkeypatch.setattr(F, "_sdc_supported", lambda chain: True)
    F.place_and_route(
        "a.json",
        "b.json",
        sdc_in="constr/fir_block_nextpnr.sdc",
        chain=RR.Toolchain(wrapper=None, prefix=[]),
    )
    assert "--sdc" in captured["args"]


# ---------------------------------------------------------------------------
# Comparison table
# ---------------------------------------------------------------------------


def test_experiment_table_computes_improvement():
    results = [_result("baseline", 50.0), _result("pipe", 100.0)]
    t = F.experiment_table(results)
    assert t["baseline_fmax_mhz"] == pytest.approx(50.0)
    assert t["best_variant"] == "pipe"
    assert t["best_fmax_mhz"] == pytest.approx(100.0)
    assert t["overall_improvement_pct"] == pytest.approx(100.0)

    imp = [i for i in t["improvements"] if i["variant"] == "pipe"][0]
    assert imp["fmax_before_mhz"] == pytest.approx(50.0)
    assert imp["fmax_after_mhz"] == pytest.approx(100.0)
    assert imp["delta_mhz"] == pytest.approx(50.0)


def test_experiment_table_handles_regression():
    results = [_result("baseline", 100.0), _result("worse", 80.0)]
    t = F.experiment_table(results)
    imp = [i for i in t["improvements"] if i["variant"] == "worse"][0]
    assert imp["improvement_pct"] == pytest.approx(-20.0)


def test_experiment_table_selects_best_variant():
    results = [_result("baseline", 50.0), _result("a", 60.0), _result("b", 90.0)]
    t = F.experiment_table(results)
    assert t["best_variant"] == "b"


def test_experiment_table_skips_failed_variants_for_best():
    bad = F.VariantResult(variant=F.Variant(name="bad"), ok=False, error="boom")
    results = [_result("baseline", 50.0), bad, _result("good", 70.0)]
    t = F.experiment_table(results)
    assert t["best_variant"] == "good"


def test_experiment_table_without_baseline_has_no_improvements():
    t = F.experiment_table([_result("pipe", 100.0)])
    assert t["baseline_fmax_mhz"] is None
    assert t["improvements"] == []
    assert t["best_variant"] == "pipe"


def test_timing_driven_placement_control_is_computed():
    on = _result("pipe", 100.0, no_tmdriv=False)
    off = _result("pipe_notmdriv", 80.0, no_tmdriv=True)
    t = F.experiment_table([_result("baseline", 50.0), on, off])
    c = t["timing_driven_placement_control"]
    assert c is not None
    assert c["with_timing_driven_placement_mhz"] == pytest.approx(100.0)
    assert c["without_timing_driven_placement_mhz"] == pytest.approx(80.0)
    assert c["delta_pct"] == pytest.approx(25.0)


def test_timing_driven_placement_control_absent_when_one_run_missing():
    t = F.experiment_table([_result("baseline", 50.0), _result("pipe", 100.0)])
    assert t["timing_driven_placement_control"] is None


def test_experiment_table_is_json_serialisable():
    results = [_result("baseline", 50.0), _result("pipe", 100.0)]
    json.dumps(F.experiment_table(results))


def test_experiment_table_with_no_results():
    t = F.experiment_table([])
    assert t["rows"] == []
    assert t["baseline_fmax_mhz"] is None


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------


def test_write_reports_creates_both_files(tmp_path):
    results = [_result("baseline", 50.0), _result("pipe", 100.0)]
    paths = F.write_reports(results, outdir=tmp_path)
    assert (tmp_path / "timing_summary.json").exists()
    assert (tmp_path / "sta_report.md").exists()
    payload = json.loads((tmp_path / "timing_summary.json").read_text(encoding="utf-8"))
    assert payload["experiment"]["best_variant"] == "pipe"
    assert len(payload["variants"]) == 2


def test_write_reports_includes_extra_sections(tmp_path):
    results = [_result("baseline", 50.0)]
    F.write_reports(results, outdir=tmp_path, extra={"lec": {"proven_count": 1}})
    payload = json.loads((tmp_path / "timing_summary.json").read_text(encoding="utf-8"))
    assert payload["lec"]["proven_count"] == 1


def test_markdown_contains_the_headline_numbers(tmp_path):
    results = [_result("baseline", 50.0), _result("pipe", 100.0)]
    F.write_reports(results, outdir=tmp_path)
    md = (tmp_path / "sta_report.md").read_text(encoding="utf-8")
    assert "50.000" in md
    assert "100.000" in md
    assert "baseline" in md and "pipe" in md
    assert "100.0% improvement" in md


def test_markdown_renders_failed_variant_without_crashing():
    bad = F.VariantResult(variant=F.Variant(name="bad"), ok=False, error="boom")
    md = F.render_markdown([bad], F.experiment_table([bad]))
    assert "FAILED" in md


def test_markdown_is_deterministic():
    results = [_result("baseline", 50.0), _result("pipe", 100.0)]
    t = F.experiment_table(results)
    assert F.render_markdown(results, t) == F.render_markdown(results, t)


# ---------------------------------------------------------------------------
# LEC result classification
# ---------------------------------------------------------------------------


def test_run_lec_classifies_proven(monkeypatch, tmp_path):
    class FakeCompleted:
        returncode = 0
        stdout = "Solving problem with 1 variables\nSAT proof finished - no model found: SUCCESS!\n"
        stderr = ""
        duration_s = 1.0

        @property
        def output(self):
            return self.stdout + ("\n" + self.stderr if self.stderr else "")


    from tclab import runner as RR

    monkeypatch.setattr(F, "run_tool", lambda *a, **k: FakeCompleted())
    out = F.run_lec("flow/lec_pipe_equiv.ys", log=None, chain=RR.Toolchain(wrapper=None, prefix=[]))
    assert out["status"] == "proven"


def test_run_lec_classifies_counterexample(monkeypatch):
    class FakeCompleted:
        returncode = 1
        stdout = "SAT proof finished - model found: FAIL!\n"
        stderr = ""
        duration_s = 0.5

        @property
        def output(self):
            return self.stdout + ("\n" + self.stderr if self.stderr else "")


    from tclab import runner as RR

    monkeypatch.setattr(F, "run_tool", lambda *a, **k: FakeCompleted())
    out = F.run_lec("x.ys", log=None, chain=RR.Toolchain(wrapper=None, prefix=[]))
    assert out["status"] == "counterexample"


def test_run_lec_classifies_unproven(monkeypatch):
    class FakeCompleted:
        returncode = 1
        stdout = "Found a total of 518 unproven $equiv cells.\nERROR: nope\n"
        stderr = ""
        duration_s = 2.0

        @property
        def output(self):
            return self.stdout + ("\n" + self.stderr if self.stderr else "")


    from tclab import runner as RR

    monkeypatch.setattr(F, "run_tool", lambda *a, **k: FakeCompleted())
    out = F.run_lec("x.ys", log=None, chain=RR.Toolchain(wrapper=None, prefix=[]))
    assert out["status"] == "unproven"
    assert "518" in out["detail"]


def test_run_lec_classifies_timeout(monkeypatch):
    class FakeCompleted:
        returncode = -9
        stdout = "Solving problem with 14639 variables\nTIMEOUT after 60s"
        stderr = ""
        duration_s = 60.0

        @property
        def output(self):
            return self.stdout + ("\n" + self.stderr if self.stderr else "")


    from tclab import runner as RR

    monkeypatch.setattr(F, "run_tool", lambda *a, **k: FakeCompleted())
    out = F.run_lec("x.ys", log=None, chain=RR.Toolchain(wrapper=None, prefix=[]), timeout=60)
    assert out["status"] == "timeout"


def test_run_lec_reports_unavailable_when_wrapper_broken(monkeypatch):
    from tclab import runner as RR

    def boom(*a, **k):
        raise RR.ToolUnavailable("no tools")

    monkeypatch.setattr(F, "run_tool", boom)
    out = F.run_lec("x.ys", log=None, chain=RR.Toolchain(wrapper=None, prefix=[]))
    assert out["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Simulation helper error path
# ---------------------------------------------------------------------------


def test_run_simulation_reports_error_when_tools_missing(monkeypatch):
    from tclab import runner as RR

    def boom(*a, **k):
        raise RR.ToolUnavailable("no iverilog")

    monkeypatch.setattr(F, "run_tool", boom)
    out = F.run_simulation(chain=RR.Toolchain(wrapper=None, prefix=[]))
    assert out["ok"] is False
    assert "no iverilog" in out["error"]


def test_run_simulation_parses_a_pass_verdict(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = "TB_PASS LAT=1 checks=720 errors=0\n"
        stderr = ""
        duration_s = 0.1

        @property
        def output(self):
            return self.stdout + ("\n" + self.stderr if self.stderr else "")


    from tclab import runner as RR

    monkeypatch.setattr(F, "run_tool", lambda *a, **k: FakeCompleted())
    out = F.run_simulation(chain=RR.Toolchain(wrapper=None, prefix=[]))
    assert out["ok"] is True
    assert out["checks"] == 720
    assert out["errors"] == 0


def test_run_simulation_parses_a_fail_verdict(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = "TB_FAIL LAT=1 checks=720 errors=674\n"
        stderr = ""
        duration_s = 0.1

        @property
        def output(self):
            return self.stdout + ("\n" + self.stderr if self.stderr else "")


    from tclab import runner as RR

    monkeypatch.setattr(F, "run_tool", lambda *a, **k: FakeCompleted())
    out = F.run_simulation(chain=RR.Toolchain(wrapper=None, prefix=[]))
    assert out["ok"] is False
    assert out["errors"] == 674
