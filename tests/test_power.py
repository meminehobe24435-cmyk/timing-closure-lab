"""Tests for tclab.power -- VCD toggle-rate extraction and the relative power
model.

Most tests use small synthetic VCDs whose toggle counts can be worked out by
hand, so the assertions are exact rather than "looks about right". One test runs
against the real activity dump committed under ``samples/``.
"""

from __future__ import annotations

import math

import pytest

from tclab import power as P


# ---------------------------------------------------------------------------
# A small VCD with hand-counted activity
# ---------------------------------------------------------------------------
#
#   clk    0 1 0 1 0 1 0 1 0 1  -> 9 transitions
#   rst_n  0 ................. 1  -> 1 transition
#   din    0000 .. 0011 .. 0000  -> 4 bit flips over 2 value changes
#
# Total: 14 bit toggles. Simulation ends at t = 90 (1 ns units).
SIMPLE_VCD = """$date Mon Jan 1 2024 $end
$version test $end
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 " rst_n $end
$var wire 4 # din [3:0] $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
0"
b0000 #
$end
#10
1!
#20
0!
#30
1!
b0011 #
#40
0!
#50
1!
b0000 #
#60
0!
1"
#70
1!
#80
0!
#90
1!
"""


def test_parse_vcd_finds_all_signals():
    a = P.parse_vcd(SIMPLE_VCD)
    names = sorted(s.full_name for s in a.signals.values())
    assert names == ["tb.clk", "tb.din[3:0]", "tb.rst_n"]


def test_parse_vcd_timescale_and_duration():
    a = P.parse_vcd(SIMPLE_VCD)
    assert a.timescale_s == pytest.approx(1e-9)
    assert a.end_time_units == 90
    assert a.duration_ns == pytest.approx(90.0)


def test_parse_vcd_signal_widths():
    a = P.parse_vcd(SIMPLE_VCD)
    by = {s.full_name: s for s in a.signals.values()}
    assert by["tb.clk"].width == 1
    assert by["tb.din[3:0]"].width == 4


def test_clk_toggle_count_is_exact():
    a = P.parse_vcd(SIMPLE_VCD)
    st = a.by_name("tb.clk")
    assert st is not None
    assert st.value_changes == 9
    assert st.bit_toggles == 9


def test_bus_bit_toggles_count_bit_flips_not_value_changes():
    a = P.parse_vcd(SIMPLE_VCD)
    st = a.by_name("tb.din[3:0]")
    # 0000->0011 is two bit flips, 0011->0000 is two more
    assert st.value_changes == 2
    assert st.bit_toggles == 4


def test_reset_toggle_count():
    a = P.parse_vcd(SIMPLE_VCD)
    st = a.by_name("tb.rst_n")
    assert st.bit_toggles == 1


def test_total_bit_toggles():
    a = P.parse_vcd(SIMPLE_VCD)
    assert a.total_bit_toggles == 14


def test_initial_values_are_not_counted_as_transitions():
    # $dumpvars establishes initial conditions; nothing has toggled yet at t=0.
    a = P.parse_vcd(SIMPLE_VCD)
    a2 = P.parse_vcd("$timescale 1ns $end\n$scope module t $end\n$var wire 4 ! x [3:0] $end\n"
                     "$upscope $end\n$enddefinitions $end\n$dumpvars\nb1111 !\n$end\n#10\n")
    assert a2.stats[next(iter(a2.stats))].bit_toggles == 0
    assert a.total_bit_toggles > 0


def test_toggle_rate_uses_duration():
    a = P.parse_vcd(SIMPLE_VCD)
    st = a.by_name("tb.clk")
    # 9 toggles in 90 ns
    assert st.toggle_rate_hz(90.0) == pytest.approx(9 / 90e-9)


def test_activity_factor_is_half_toggles_per_cycle():
    st = P.ToggleStat(name="x", width=1, bit_toggles=100)
    # 100 toggles over 50 cycles -> alpha = 1.0 (toggles every cycle)
    assert st.activity_factor(500.0, 100.0) == pytest.approx(1.0)


def test_activity_factor_zero_duration():
    st = P.ToggleStat(name="x", width=1, bit_toggles=10)
    assert st.activity_factor(0.0, 100.0) == 0.0
    assert st.toggle_rate_hz(0.0) == 0.0


def test_toggling_only_filters_static_signals():
    vcd = SIMPLE_VCD
    a = P.parse_vcd(vcd)
    assert len(a.toggling_only()) == 3
    assert len(a.toggling_only(minimum=5)) == 1


def test_top_toggling_is_sorted():
    a = P.parse_vcd(SIMPLE_VCD)
    top = a.top_toggling(3)
    assert [s.bit_toggles for s in top] == sorted([s.bit_toggles for s in top], reverse=True)


def test_activity_dict_is_json_serialisable():
    import json

    a = P.parse_vcd(SIMPLE_VCD)
    json.dumps(a.as_dict())


# ---------------------------------------------------------------------------
# VCD edge cases
# ---------------------------------------------------------------------------


def test_x_and_z_values_are_not_counted_as_toggles():
    """A transition into or out of an unknown value cannot be counted honestly.

    From 0 the signal goes to x, then to 1, then to z. The 0->x step is not a
    known switching event and the x->1 step does not tell you how many bits
    moved, so the honest count is zero rather than three or one.
    """
    vcd = (
        "$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! a $end\n$upscope $end\n"
        "$enddefinitions $end\n$dumpvars\n0!\n$end\n#10\nx!\n#20\n1!\n#30\nz!\n"
    )
    a = P.parse_vcd(vcd)
    st = a.stats[next(iter(a.stats))]
    assert st.bit_toggles == 0


def test_known_transitions_after_an_unknown_are_still_counted():
    vcd = (
        "$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! a $end\n$upscope $end\n"
        "$enddefinitions $end\n$dumpvars\n0!\n$end\n#10\nx!\n#20\n1!\n#30\n0!\n"
    )
    a = P.parse_vcd(vcd)
    st = a.stats[next(iter(a.stats))]
    # only the known 1 -> 0 transition counts
    assert st.bit_toggles == 1


def test_dumpoff_resets_known_values():
    vcd = (
        "$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! a $end\n$upscope $end\n"
        "$enddefinitions $end\n$dumpvars\n0!\n$end\n#10\n1!\n#20\n$dumpoff\n$end\n"
        "#30\n1!\n"
    )
    a = P.parse_vcd(vcd)
    st = a.stats[next(iter(a.stats))]
    # only the 0->1 at #10 counts; after $dumpoff the previous value is forgotten
    assert st.bit_toggles == 1


def test_unknown_signal_id_is_ignored():
    vcd = "$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! a $end\n$upscope $end\n$enddefinitions $end\n#10\n1?\n"
    a = P.parse_vcd(vcd)
    assert a.total_bit_toggles == 0


def test_empty_vcd_is_tolerated():
    a = P.parse_vcd("")
    assert a.signals == {}
    assert a.total_bit_toggles == 0
    assert a.duration_ns == 0.0


def test_timescale_with_magnitude_is_parsed():
    a = P.parse_vcd("$timescale 10 ps $end\n$enddefinitions $end\n#5\n")
    assert a.timescale_s == pytest.approx(10e-12)
    assert a.duration_ns == pytest.approx(0.05)


def test_max_changes_truncates_parsing():
    a = P.parse_vcd(SIMPLE_VCD, max_changes=3)
    assert a.total_value_changes <= 3


def test_missing_vcd_file_raises(tmp_path):
    with pytest.raises(P.VcdParseError):
        P.parse_vcd_file(tmp_path / "nope.vcd")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_classify_recognises_clock_reset_and_data():
    m = P.PowerModel()
    assert m.classify("tb.clk") == "clock"
    assert m.classify("u_div.clk_div2_q") == "clock"
    assert m.classify("tb.rst_n") == "reset"
    assert m.classify("u_core.sr[3][7]") == "data"


def test_clock_weight_exceeds_data_weight():
    m = P.PowerModel()
    assert m.weight("clk") > m.weight("din")


def test_estimate_relative_power_matches_hand_calculation():
    a = P.parse_vcd(SIMPLE_VCD)
    m = P.PowerModel()
    e = P.estimate(a, m, label="simple")
    dur = a.duration_ns
    expected_clock = 4.0 * (9 / (dur * 1e-9))
    expected_reset = 1.5 * (1 / (dur * 1e-9))
    expected_data = 1.0 * (4 / (dur * 1e-9))
    assert e.by_class["clock"] == pytest.approx(expected_clock)
    assert e.by_class["reset"] == pytest.approx(expected_reset)
    assert e.by_class["data"] == pytest.approx(expected_data)
    assert e.relative_power == pytest.approx(expected_clock + expected_reset + expected_data)


def test_estimate_counts_only_toggling_nets():
    a = P.parse_vcd(SIMPLE_VCD)
    e = P.estimate(a, label="x")
    assert e.toggling_nets == 3


def test_estimate_of_static_design_is_zero():
    a = P.parse_vcd(
        "$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! a $end\n$upscope $end\n"
        "$enddefinitions $end\n$dumpvars\n0!\n$end\n#100\n"
    )
    e = P.estimate(a)
    assert e.relative_power == 0.0
    assert e.toggling_nets == 0


def test_estimate_records_the_model_note():
    a = P.parse_vcd(SIMPLE_VCD)
    e = P.estimate(a)
    assert "signoff" in e.model_note.lower()
    assert "P_rel" in e.model_note


def test_estimate_dict_is_json_serialisable():
    import json

    a = P.parse_vcd(SIMPLE_VCD)
    json.dumps(P.estimate(a).as_dict())


def test_weight_by_width_option_changes_result():
    a = P.parse_vcd(SIMPLE_VCD)
    plain = P.estimate(a, P.PowerModel())
    wide = P.estimate(a, P.PowerModel(weight_by_width=True))
    assert wide.relative_power > plain.relative_power


def test_custom_weights_scale_the_result():
    a = P.parse_vcd(SIMPLE_VCD)
    base = P.estimate(a, P.PowerModel())
    doubled = P.estimate(a, P.PowerModel(weights={"clock": 8.0, "reset": 1.5, "data": 1.0}))
    assert doubled.relative_power > base.relative_power


def test_compare_ratio_and_delta():
    a = P.parse_vcd(SIMPLE_VCD)
    ref = P.estimate(a, label="ref")
    half = P.PowerEstimate(label="half", relative_power=ref.relative_power / 2, by_class=dict(ref.by_class))
    c = P.compare(ref, half)
    assert c["ratio"] == pytest.approx(0.5)
    assert c["delta_pct"] == pytest.approx(-50.0)


def test_compare_handles_zero_reference():
    zero = P.PowerEstimate(label="zero", relative_power=0.0)
    other = P.PowerEstimate(label="other", relative_power=5.0)
    c = P.compare(zero, other)
    assert c["ratio"] == 0.0
    assert c["delta_pct"] == 0.0


def test_compare_reports_per_class_ratios():
    ref = P.PowerEstimate(label="r", relative_power=10.0, by_class={"clock": 4.0, "data": 6.0})
    cand = P.PowerEstimate(label="c", relative_power=5.0, by_class={"clock": 4.0, "data": 1.0})
    c = P.compare(ref, cand)
    assert c["class_ratios"]["clock"] == pytest.approx(1.0)
    assert c["class_ratios"]["data"] == pytest.approx(1 / 6)


# ---------------------------------------------------------------------------
# Real activity dump
# ---------------------------------------------------------------------------


def test_real_activity_vcd_parses(sample_vcd):
    a = P.parse_vcd(sample_vcd)
    assert a.signals
    assert a.duration_ns > 0
    assert a.total_bit_toggles > 0


def test_real_activity_vcd_has_a_clock_signal(sample_vcd):
    a = P.parse_vcd(sample_vcd)
    clocks = [s for s in a.stats.values() if P.PowerModel().classify(s.name) == "clock"]
    assert clocks
