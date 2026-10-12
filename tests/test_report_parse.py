"""Tests for tclab.report_parse -- the nextpnr JSON report reader.

Includes the degenerate inputs the flow actually produces: an empty report, a
report missing whole sections, ``null`` where an object is expected, malformed
endpoint delay pairs, and a report that is not JSON at all.
"""

from __future__ import annotations

import json

import pytest

from tclab import report_parse as R


# ---------------------------------------------------------------------------
# Cell-name normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("u_core.sr[11]_TRELLIS_FF_Q_3", "u_core.sr[11]"),
        ("u_core.prod[11]_MULT18X18D_P9", "u_core.prod[11]"),
        ("dout[0]$tr_io", "dout[0]"),
        ("clk$TRELLIS_IO_IN", "clk"),
        ("rst_n$tr_io", "rst_n"),
        ("u_core.g_chain.chain[15]_CCU2C_S1_7$CCU2_COMB1", "u_core.g_chain.chain[15]"),
        ("u_core.dout_TRELLIS_FF_Q_8", "u_core.dout"),
        ("", ""),
    ],
)
def test_normalize_cell_name(raw, expected):
    assert R.normalize_cell_name(raw) == expected


def test_normalize_cell_name_handles_none():
    assert R.normalize_cell_name(None) == ""


# ---------------------------------------------------------------------------
# Utilisation
# ---------------------------------------------------------------------------


def test_utilization_accessors(small_report):
    rep = R.parse_report(small_report)
    u = rep.utilization
    assert u.lut4 == u.used("TRELLIS_COMB")
    assert u.ff == u.used("TRELLIS_FF")
    assert u.dsp == u.used("MULT18X18D")
    assert u.bram == u.used("DP16KD")


def test_utilization_pct_and_missing_cell():
    rep = R.parse_report({"utilization": {"TRELLIS_COMB": {"used": 10, "available": 100}}})
    assert rep.utilization.pct("TRELLIS_COMB") == pytest.approx(10.0)
    assert rep.utilization.used("NOPE") == 0
    assert rep.utilization.pct("NOPE") == 0.0


def test_utilization_nonzero_filters_zeros():
    rep = R.parse_report(
        {"utilization": {"A": {"used": 0, "available": 5}, "B": {"used": 2, "available": 5}}}
    )
    assert set(rep.utilization.nonzero()) == {"B"}


# ---------------------------------------------------------------------------
# Fmax
# ---------------------------------------------------------------------------


def test_fmax_entries_parse(small_report):
    rep = R.parse_report(small_report)
    assert rep.fmax
    e = rep.fmax[0]
    assert e.achieved_mhz > 0
    assert e.constraint_mhz > 0
    assert e.achieved_period_ns == pytest.approx(1000.0 / e.achieved_mhz)


def test_fmax_met_flag():
    rep = R.parse_report({"fmax": {"c": {"achieved": 120.0, "constraint": 100.0}}})
    assert rep.fmax[0].is_met
    assert rep.fmax[0].margin_mhz == pytest.approx(20.0)


def test_fmax_not_met_flag():
    rep = R.parse_report({"fmax": {"c": {"achieved": 50.0, "constraint": 100.0}}})
    assert not rep.fmax[0].is_met


def test_primary_fmax_picks_highest_constraint():
    rep = R.parse_report(
        {
            "fmax": {
                "slow": {"achieved": 40.0, "constraint": 50.0},
                "fast": {"achieved": 80.0, "constraint": 100.0},
            }
        }
    )
    assert rep.primary_fmax.clock == "fast"


def test_fmax_missing_achieved_degrades_to_zero():
    rep = R.parse_report({"fmax": {"c": {"constraint": 100.0}}})
    assert rep.fmax[0].achieved_mhz == 0.0
    assert rep.fmax[0].achieved_period_ns == float("inf")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_critical_path_totals_sum_the_steps(full_report):
    rep = R.parse_report(full_report)
    w = rep.worst_clocked_path()
    assert w is not None
    assert w.total_ns == pytest.approx(sum(s.delay_ns for s in w.steps))
    # The tool's own Fmax must be reproducible from the extracted path.
    assert w.implied_fmax_mhz == pytest.approx(rep.primary_fmax.achieved_mhz, rel=1e-6)


def test_critical_path_delay_types_split(full_report):
    rep = R.parse_report(full_report)
    w = rep.worst_clocked_path()
    d = w.delay_types()
    assert set(d) == {"logic_ns", "routing_ns", "clk_to_q_ns", "setup_ns", "source_ns"}
    assert d["logic_ns"] + d["routing_ns"] + d["clk_to_q_ns"] <= w.total_ns + 1e-9


def test_critical_path_endpoints_normalised(full_report):
    rep = R.parse_report(full_report)
    w = rep.worst_clocked_path()
    assert "_TRELLIS_FF" not in w.start_point
    assert "_TRELLIS_FF" not in w.end_point
    assert w.start_point and w.end_point


def test_clocked_path_filter_excludes_io_paths(full_report):
    rep = R.parse_report(full_report)
    clocked = rep.clocked_paths()
    assert clocked
    for p in clocked:
        assert p.is_clocked
    assert len(clocked) <= len(rep.critical_paths)


def test_culprit_nets_are_routing_steps_sorted(full_report):
    rep = R.parse_report(full_report)
    nets = rep.worst_clocked_path().culprit_nets(5)
    assert nets
    delays = [d for _, d in nets]
    assert delays == sorted(delays, reverse=True)


def test_path_missing_steps_degrades_to_zero_delay():
    rep = R.parse_report({"critical_paths": [{"from": "a", "to": "b"}]})
    p = rep.critical_paths[0]
    assert p.steps == []
    assert p.total_ns == 0.0
    assert p.implied_fmax_mhz == float("inf")


def test_path_step_missing_delay_defaults_to_zero():
    rep = R.parse_report(
        {"critical_paths": [{"from": "a", "to": "b", "path": [{"type": "logic"}]}]}
    )
    assert rep.critical_paths[0].steps[0].delay_ns == 0.0


def test_path_step_null_from_to_does_not_raise():
    rep = R.parse_report(
        {
            "critical_paths": [
                {"from": "a", "to": "b", "path": [{"type": "logic", "delay": 1.0, "from": None, "to": None}]}
            ]
        }
    )
    s = rep.critical_paths[0].steps[0]
    assert s.from_cell == "" and s.to_cell == ""


# ---------------------------------------------------------------------------
# Net timings
# ---------------------------------------------------------------------------


def test_net_timing_endpoint_min_max_pair(full_report):
    rep = R.parse_report(full_report)
    nt = rep.net_timings[0]
    ep = nt.endpoints[0]
    assert ep.min_ns is not None and ep.max_ns is not None
    assert ep.max_ns >= ep.min_ns


def test_net_timing_scalar_delay_is_used_for_both_bounds():
    rep = R.parse_report(
        {"detailed_net_timings": [{"net": "n", "endpoints": [{"cell": "c", "delay": 3.5}]}]}
    )
    ep = rep.net_timings[0].endpoints[0]
    assert ep.min_ns == 3.5 and ep.max_ns == 3.5


def test_net_timing_single_element_delay_list():
    rep = R.parse_report(
        {"detailed_net_timings": [{"net": "n", "endpoints": [{"cell": "c", "delay": [2.0]}]}]}
    )
    ep = rep.net_timings[0].endpoints[0]
    assert ep.min_ns == 2.0 and ep.max_ns == 2.0


def test_net_timing_fanout_counts_endpoints(full_report):
    rep = R.parse_report(full_report)
    assert any(nt.fanout > 1 for nt in rep.net_timings)


def test_net_timing_empty_endpoints_list():
    rep = R.parse_report({"detailed_net_timings": [{"net": "n", "endpoints": []}]})
    assert rep.net_timings[0].fanout == 0
    assert rep.net_timings[0].worst_arrival_ns is None


def test_worst_arrival_by_endpoint_sorted(full_report):
    rep = R.parse_report(full_report)
    rows = rep.worst_arrival_by_endpoint(5)
    assert rows
    assert [r[1] for r in rows] == sorted((r[1] for r in rows), reverse=True)


def test_worst_drivers_sorted(full_report):
    rep = R.parse_report(full_report)
    rows = rep.worst_drivers(5)
    assert rows
    assert [r[1] for r in rows] == sorted((r[1] for r in rows), reverse=True)


# ---------------------------------------------------------------------------
# Degenerate / malformed reports
# ---------------------------------------------------------------------------


def test_empty_object_report_is_empty():
    rep = R.parse_report({})
    assert rep.is_empty()
    assert rep.fmax == []
    assert rep.critical_paths == []
    assert rep.net_timings == []
    assert rep.utilization.cells == {}


def test_empty_report_can_be_analysed_without_raising():
    from tclab import sta

    rep = R.parse_report({})
    s = sta.analyze(rep)
    assert s.clocks == []
    assert s.critical_path is None
    assert s.worst_achieved_mhz == 0.0
    assert s.timing_met  # vacuously true: no clock to violate


def test_report_with_null_sections_is_tolerated():
    rep = R.parse_report(
        {"fmax": None, "utilization": None, "critical_paths": None, "detailed_net_timings": None}
    )
    assert rep.is_empty()


def test_report_with_wrong_typed_sections_is_tolerated():
    rep = R.parse_report(
        {"fmax": [], "utilization": 3, "critical_paths": "nope", "detailed_net_timings": {}}
    )
    assert rep.is_empty()


def test_null_entries_inside_lists_are_skipped():
    rep = R.parse_report(
        {"critical_paths": [None, {}, {"from": "a", "to": "b", "path": []}], "detailed_net_timings": [None, {}]}
    )
    assert len(rep.critical_paths) == 1
    assert len(rep.net_timings) == 0


def test_utilization_entry_with_garbage_is_skipped():
    rep = R.parse_report({"utilization": {"A": "not a dict", "B": {"used": 1, "available": 2}}})
    assert "A" not in rep.utilization.cells
    assert rep.utilization.used("B") == 1


def test_invalid_json_raises_reporterror():
    with pytest.raises(R.ReportError):
        R.parse_report("{ this is not json ")


def test_json_array_root_raises_reporterror():
    with pytest.raises(R.ReportError):
        R.parse_report("[1, 2, 3]")


def test_unsupported_input_type_raises_reporterror():
    with pytest.raises(R.ReportError):
        R.parse_report(12345)  # type: ignore[arg-type]


def test_missing_file_raises_reporterror(tmp_path):
    with pytest.raises(R.ReportError):
        R.parse_report_file(tmp_path / "does_not_exist.json")


def test_parse_report_file_roundtrip(tmp_path, small_report):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(small_report), encoding="utf-8")
    rep = R.parse_report_file(p)
    assert rep.source == str(p)
    assert not rep.is_empty()


def test_report_accepts_raw_json_text(small_report):
    rep = R.parse_report(json.dumps(small_report))
    assert rep.fmax


def test_non_numeric_delay_string_is_zero():
    rep = R.parse_report(
        {"critical_paths": [{"from": "a", "to": "b", "path": [{"type": "logic", "delay": "NaN"}]}]}
    )
    assert rep.critical_paths[0].steps[0].delay_ns == 0.0


def test_numeric_delay_string_is_parsed():
    rep = R.parse_report(
        {"critical_paths": [{"from": "a", "to": "b", "path": [{"type": "logic", "delay": "1.25"}]}]}
    )
    assert rep.critical_paths[0].steps[0].delay_ns == pytest.approx(1.25)
