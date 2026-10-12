"""Tests for tclab.sta -- Fmax, slack, critical-path extraction and bottleneck
ranking.

The numbers in the arithmetic tests are computed independently in the test, so a
sign error or an inverted ratio in the implementation shows up as a failure
rather than as a plausible-looking number.
"""

from __future__ import annotations

import math

import pytest

from tclab import report_parse as R
from tclab import sta


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delay,expected",
    [(10.0, 100.0), (20.0, 50.0), (5.0, 200.0), (19.308, 51.79200293944002)],
)
def test_fmax_mhz(delay, expected):
    assert sta.fmax_mhz(delay) == pytest.approx(expected)


def test_fmax_of_zero_delay_is_infinite():
    assert sta.fmax_mhz(0.0) == math.inf


def test_fmax_of_negative_delay_is_infinite():
    assert sta.fmax_mhz(-1.0) == math.inf


@pytest.mark.parametrize("freq,expected", [(100.0, 10.0), (50.0, 20.0), (200.0, 5.0)])
def test_period_ns(freq, expected):
    assert sta.period_ns(freq) == pytest.approx(expected)


def test_period_of_zero_frequency_is_infinite():
    assert sta.period_ns(0.0) == math.inf


def test_setup_slack_positive_when_path_fits():
    assert sta.setup_slack_ns(10.0, 8.5) == pytest.approx(1.5)


def test_setup_slack_negative_when_path_is_too_long():
    assert sta.setup_slack_ns(10.0, 19.308) == pytest.approx(-9.308)


def test_fmax_and_slack_are_consistent():
    # A path that exactly consumes the period must have zero slack.
    period = sta.period_ns(80.0)
    assert sta.setup_slack_ns(period, period) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ClockAnalysis
# ---------------------------------------------------------------------------


def _clock(achieved, constraint):
    return sta.ClockAnalysis(
        clock="c", constraint_mhz=constraint, achieved_mhz=achieved, worst_path_ns=sta.period_ns(achieved)
    )


def test_clock_analysis_met_when_achieved_above_constraint():
    c = _clock(120.0, 100.0)
    assert c.met
    assert c.setup_slack_ns > 0
    assert c.headroom_mhz == pytest.approx(20.0)


def test_clock_analysis_violated_when_achieved_below_constraint():
    c = _clock(51.792, 100.0)
    assert not c.met
    assert c.setup_slack_ns < 0


def test_clock_analysis_dict_is_json_serialisable():
    import json

    json.dumps(_clock(100.0, 100.0).as_dict())


# ---------------------------------------------------------------------------
# Analysis of real and synthetic reports
# ---------------------------------------------------------------------------


def test_analyze_real_report_has_a_primary_clock(full_report):
    s = sta.analyze(R.parse_report(full_report))
    assert s.primary is not None
    assert s.primary.constraint_mhz > 0


def test_analyze_real_report_critical_path_matches_reported_fmax(full_report):
    s = sta.analyze(R.parse_report(full_report))
    assert s.critical_path is not None
    assert s.critical_path["implied_fmax_mhz"] == pytest.approx(s.primary.achieved_mhz, rel=1e-6)


def test_analyze_populates_all_bottleneck_lists(full_report):
    s = sta.analyze(R.parse_report(full_report), top_n=5)
    assert s.bottlenecks_by_endpoint
    assert s.bottlenecks_by_start
    assert s.nets_by_delay
    assert all(len(x) <= 5 for x in (s.bottlenecks_by_endpoint, s.bottlenecks_by_start, s.nets_by_delay))


def test_bottleneck_lists_are_ranked_descending(full_report):
    s = sta.analyze(R.parse_report(full_report))
    for lst in (s.bottlenecks_by_endpoint, s.bottlenecks_by_start, s.nets_by_delay):
        vals = [b.worst_arrival_ns for b in lst]
        assert vals == sorted(vals, reverse=True)


def test_delay_split_sums_to_path_total(full_report):
    s = sta.analyze(R.parse_report(full_report))
    d = s.delay_split
    total = sum(d.values())
    assert total == pytest.approx(s.critical_path["total_ns"], rel=1e-9)


def test_hold_is_reported_as_not_analysed(full_report):
    s = sta.analyze(R.parse_report(full_report))
    assert s.hold_analysed is False
    assert "hold" in s.hold_note.lower()


def test_conclusions_mention_violation_when_slack_negative(full_report):
    s = sta.analyze(R.parse_report(full_report))
    if not s.timing_met:
        assert any("VIOLATED" in c for c in s.conclusions)


def test_conclusions_mention_met_when_slack_positive():
    rep = R.parse_report(
        {
            "fmax": {"clk": {"achieved": 200.0, "constraint": 100.0}},
            "critical_paths": [
                {
                    "from": "posedge clk",
                    "to": "posedge clk",
                    "path": [
                        {"type": "clk-to-q", "delay": 0.5, "from": {"cell": "a"}, "to": {"cell": "a"}},
                        {"type": "logic", "delay": 1.0, "from": {"cell": "a"}, "to": {"cell": "b"}},
                        {"type": "routing", "delay": 1.0, "from": {"cell": "b"}, "to": {"cell": "c"}},
                        {"type": "setup", "delay": 0.0, "from": {"cell": "c"}, "to": {"cell": "c"}},
                    ],
                }
            ],
        }
    )
    s = sta.analyze(rep)
    assert s.timing_met
    assert any("MET" in c for c in s.conclusions)


def test_conclusion_flags_routing_dominated_path():
    # 1 ns logic vs 9 ns routing should trigger the placement/congestion message.
    steps = [{"type": "logic", "delay": 1.0, "from": {"cell": "a"}, "to": {"cell": "b"}}]
    steps += [{"type": "routing", "delay": 3.0, "from": {"cell": "b"}, "to": {"cell": "c"}} for _ in range(3)]
    steps.append({"type": "setup", "delay": 0.0, "from": {"cell": "c"}, "to": {"cell": "c"}})
    rep = R.parse_report(
        {
            "fmax": {"clk": {"achieved": 100.0, "constraint": 100.0}},
            "critical_paths": [{"from": "posedge clk", "to": "posedge clk", "path": steps}],
        }
    )
    s = sta.analyze(rep)
    assert any("Routing dominates" in c for c in s.conclusions)


def test_conclusion_flags_logic_dominated_path():
    steps = [{"type": "logic", "delay": 5.0, "from": {"cell": "a"}, "to": {"cell": "b"}}]
    steps += [{"type": "routing", "delay": 0.5, "from": {"cell": "b"}, "to": {"cell": "c"}}]
    steps.append({"type": "setup", "delay": 0.0, "from": {"cell": "c"}, "to": {"cell": "c"}})
    rep = R.parse_report(
        {
            "fmax": {"clk": {"achieved": 100.0, "constraint": 100.0}},
            "critical_paths": [{"from": "posedge clk", "to": "posedge clk", "path": steps}],
        }
    )
    s = sta.analyze(rep)
    assert any("Logic delay dominates" in c for c in s.conclusions)


def test_analyze_uses_larger_of_tool_and_extracted_path():
    """A mis-extracted short path must not flatter the design's Fmax."""
    rep = R.parse_report(
        {
            "fmax": {"clk": {"achieved": 100.0, "constraint": 100.0}},
            "critical_paths": [
                {
                    "from": "posedge clk",
                    "to": "posedge clk",
                    "path": [{"type": "logic", "delay": 20.0, "from": {"cell": "a"}, "to": {"cell": "b"}}],
                }
            ],
        }
    )
    s = sta.analyze(rep)
    # tool says 100 MHz (10 ns) but the extracted path is 20 ns -> report 50 MHz
    assert s.primary.achieved_mhz == pytest.approx(50.0)
    assert not s.primary.met


def test_analyze_dict_is_json_serialisable(full_report):
    import json

    s = sta.analyze(R.parse_report(full_report))
    json.dumps(s.as_dict())


def test_top_n_zero_yields_empty_bottleneck_lists(full_report):
    s = sta.analyze(R.parse_report(full_report), top_n=0)
    assert s.bottlenecks_by_endpoint == []
    assert s.bottlenecks_by_start == []
    assert s.nets_by_delay == []


def test_unused_sdc_argument_is_accepted(full_report):
    from tclab import sdc as S

    d = S.parse_file("constr/fir_block.sdc")
    s = sta.analyze(R.parse_report(full_report), sdc=d)
    assert s.primary is not None
