"""Tests for tclab.sdc -- the TCL/SDC subset parser.

Coverage: happy path, brace grouping, command substitution, variables,
continuation lines, comments, and malformed input that must produce a
line-numbered error rather than a silent misparse.
"""

from __future__ import annotations

import pytest

from tclab import sdc as S


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_clock_parses_name_and_period():
    d = S.parse("create_clock -name sys_clk -period 10.0 [get_ports clk]")
    assert len(d.clocks) == 1
    c = d.clocks[0]
    assert c.name == "sys_clk"
    assert c.period_ns == 10.0
    assert c.frequency_mhz == pytest.approx(100.0)


def test_create_clock_without_name_derives_it_from_target():
    d = S.parse("create_clock -period 5.0 [get_ports clk]")
    assert d.clocks[0].name == "clk"
    assert d.clocks[0].frequency_mhz == pytest.approx(200.0)


def test_create_clock_target_selector_kind_and_patterns():
    d = S.parse("create_clock -period 4 [get_ports clk]")
    target = d.clocks[0].target
    assert isinstance(target, S.Selector)
    assert target.kind == "ports"
    assert target.patterns == ("clk",)


def test_create_generated_clock_divide_by_derives_period():
    src = (
        "create_clock -name sys_clk -period 10.0 [get_ports clk]\n"
        "create_generated_clock -name clk_div2 -source [get_ports clk] "
        "-divide_by 2 [get_pins u_div/q]\n"
    )
    d = S.parse(src)
    assert len(d.generated_clocks) == 1
    g = d.generated_clocks[0]
    assert g.divide_by == 2
    assert g.period_ns(d.clocks) == pytest.approx(20.0)


def test_create_generated_clock_multiply_by_derives_period():
    src = (
        "create_clock -name c -period 20.0 [get_ports clk]\n"
        "create_generated_clock -name c2 -source [get_ports clk] "
        "-multiply_by 4 [get_pins u/q]\n"
    )
    d = S.parse(src)
    assert d.generated_clocks[0].period_ns(d.clocks) == pytest.approx(5.0)


def test_generated_clock_without_ratio_is_an_error():
    with pytest.raises(S.SdcSyntaxError) as ei:
        S.parse("create_generated_clock -name c -source [get_ports clk] [get_pins u/q]")
    assert "-divide_by" in str(ei.value)


def test_input_and_output_delays_are_separated():
    src = (
        "set_input_delay -clock sys_clk -max 2.0 [get_ports {din[*]}]\n"
        "set_output_delay -clock sys_clk -max 3.0 [get_ports {dout[*]}]\n"
    )
    d = S.parse(src)
    assert len(d.input_delays) == 1
    assert len(d.output_delays) == 1
    assert d.input_delays[0].max_ns == 2.0
    assert d.output_delays[0].kind == "output"


def test_io_delay_min_only_is_allowed():
    d = S.parse("set_input_delay -clock c -min 0.5 [get_ports din]")
    assert d.input_delays[0].min_ns == 0.5
    assert d.input_delays[0].max_ns is None


def test_io_delay_without_max_or_min_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse("set_input_delay -clock c [get_ports din]")


def test_max_delay_value_and_endpoints():
    d = S.parse("set_max_delay 6.0 -from [get_ports {din[*]}] -to [get_ports {dout[*]}]")
    assert d.max_delays[0].value_ns == 6.0
    assert isinstance(d.max_delays[0].frm, S.Selector)


def test_false_path_from_only():
    d = S.parse("set_false_path -from [get_ports rst_n]")
    fp = d.false_paths[0]
    assert fp.frm is not None
    assert fp.to is None


def test_multicycle_setup_and_hold():
    d = S.parse("set_multicycle_path -setup 2 -hold 1 -from [get_ports a] -to [get_ports b]")
    m = d.multicycle_paths[0]
    assert m.setup == 2
    assert m.hold == 1


def test_multicycle_without_setup_or_hold_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse("set_multicycle_path -from [get_ports a] -to [get_ports b]")


def test_clock_groups_two_groups():
    d = S.parse("set_clock_groups -asynchronous -group {sys_clk} -group {clk_div2}")
    cg = d.clock_groups[0]
    assert cg.mode == "asynchronous"
    assert cg.groups == [["sys_clk"], ["clk_div2"]]


def test_clock_groups_group_with_multiple_members():
    d = S.parse("set_clock_groups -exclusive -group {a b} -group {c}")
    assert d.clock_groups[0].groups == [["a", "b"], ["c"]]


def test_clock_groups_without_mode_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse("set_clock_groups -group {a} -group {b}")


def test_clock_groups_without_group_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse("set_clock_groups -asynchronous a b")


def test_clock_groups_conflicting_modes_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse("set_clock_groups -asynchronous -exclusive -group {a} -group {b}")


# ---------------------------------------------------------------------------
# TCL language features
# ---------------------------------------------------------------------------


def test_braces_group_a_word_with_spaces():
    d = S.parse("set_input_delay -clock c -max 1 [get_ports {a b c}]")
    t = d.input_delays[0].target
    assert isinstance(t, S.Selector)
    assert t.patterns == ("a", "b", "c")


def test_braces_are_literal_no_substitution():
    # $undefined inside braces must NOT be substituted, and so must not error.
    d = S.parse("set_input_delay -clock c -max 1 [get_ports {$not_a_var}]")
    assert d.input_delays[0].target.patterns == ("$not_a_var",)


def test_nested_braces_are_balanced():
    d = S.parse("set_input_delay -clock c -max 1 [get_ports {a{b}c}]")
    assert d.input_delays[0].target.patterns == ("a{b}c",)


def test_command_substitution_get_ports_wildcard():
    d = S.parse("set_input_delay -clock c -max 2 [get_ports {din[*]}]")
    t = d.input_delays[0].target
    assert t.patterns == ("din[*]",)
    assert t.is_wildcard


def test_nested_command_substitution():
    d = S.parse("set_input_delay -clock c -max 2 [get_pins {u_top/u_sub/clk_div2_q}]")
    assert d.input_delays[0].target.kind == "pins"


def test_variable_set_and_substitution():
    d = S.parse("set PER 10.0\ncreate_clock -name c -period $PER [get_ports clk]")
    assert d.clocks[0].period_ns == 10.0
    assert d.variables["PER"] == "10.0"


def test_braced_variable_substitution():
    d = S.parse("set P 8\ncreate_clock -name c -period ${P} [get_ports clk]")
    assert d.clocks[0].period_ns == 8.0


def test_undefined_variable_is_a_line_numbered_error():
    with pytest.raises(S.SdcEvaluationError) as ei:
        S.parse("create_clock -name c -period $NOPE [get_ports clk]")
    assert ei.value.line == 1
    assert "NOPE" in str(ei.value)


def test_line_continuation_joins_physical_lines():
    src = "create_clock -name sys_clk \\\n  -period 10.0 \\\n  [get_ports clk]\n"
    d = S.parse(src)
    assert d.clocks[0].name == "sys_clk"
    assert d.clocks[0].period_ns == 10.0


def test_comments_are_ignored():
    src = "# leading comment\ncreate_clock -period 10 [get_ports clk]  # trailing\n# another\n"
    d = S.parse(src)
    assert len(d.clocks) == 1


def test_hash_inside_braces_is_not_a_comment():
    d = S.parse("set_input_delay -clock c -max 1 [get_ports {a#b}]")
    assert d.input_delays[0].target.patterns == ("a#b",)


def test_semicolon_separates_commands():
    d = S.parse("create_clock -period 10 [get_ports clk]; set_input_delay -clock c -max 1 [get_ports din]")
    assert len(d.clocks) == 1
    assert len(d.input_delays) == 1


def test_double_quoted_word_with_substitution():
    d = S.parse('set P 12\ncreate_clock -name "clk_$P" -period 4 [get_ports clk]')
    assert d.clocks[0].name == "clk_12"


def test_expr_arithmetic_in_substitution():
    d = S.parse("set P 4\ncreate_clock -name c -period [expr {$P * 2.5}] [get_ports clk]")
    assert d.clocks[0].period_ns == 10.0


def test_expr_division_by_zero_is_an_error():
    with pytest.raises(S.SdcEvaluationError):
        S.parse("create_clock -name c -period [expr {1/0}] [get_ports clk]")


# ---------------------------------------------------------------------------
# Malformed input -- must fail loudly with the right line
# ---------------------------------------------------------------------------


def test_unbalanced_open_brace_reports_start_line():
    with pytest.raises(S.SdcSyntaxError) as ei:
        S.parse("create_clock -period 10 [get_ports clk]\nset_input_delay -clock c -max 1 [get_ports {a b\n")
    assert ei.value.line == 2
    assert "unbalanced" in str(ei.value).lower()


def test_unbalanced_close_brace_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse("set_input_delay -clock c -max 1 [get_ports a]}")


def test_unbalanced_bracket_reports_line():
    with pytest.raises(S.SdcSyntaxError) as ei:
        S.parse("\n\ncreate_clock -period 10 [get_ports clk\n")
    assert ei.value.line == 3


def test_unbalanced_quote_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse('create_clock -name "abc -period 10 [get_ports clk]')


def test_unknown_command_raises_with_line_number():
    with pytest.raises(S.SdcUnknownCommandError) as ei:
        S.parse("create_clock -period 10 [get_ports clk]\nnot_a_command foo\n")
    assert ei.value.line == 2
    assert "not_a_command" in str(ei.value)


def test_unknown_command_allowed_in_lenient_mode():
    d = S.parse("totally_made_up 1 2 3\ncreate_clock -period 10 [get_ports clk]", strict=False)
    assert len(d.clocks) == 1
    assert "totally_made_up" in d.command_names()


def test_passthrough_commands_are_accepted_in_strict_mode():
    d = S.parse("set_clock_uncertainty 0.2 [get_clocks c]\ncreate_clock -period 10 [get_ports clk]")
    assert len(d.clocks) == 1


def test_unknown_option_to_known_command_is_an_error():
    with pytest.raises(S.SdcSyntaxError) as ei:
        S.parse("create_clock -period 10 -bogus 1 [get_ports clk]")
    assert "bogus" in str(ei.value)


def test_option_missing_its_value_is_an_error():
    with pytest.raises(S.SdcSyntaxError):
        S.parse("create_clock -name")


def test_non_numeric_period_is_an_error():
    with pytest.raises(S.SdcEvaluationError):
        S.parse("create_clock -name c -period hello [get_ports clk]")


def test_negative_period_is_rejected():
    with pytest.raises(S.SdcEvaluationError):
        S.parse("create_clock -name c -period -5 [get_ports clk]")


def test_zero_period_is_rejected():
    with pytest.raises(S.SdcEvaluationError):
        S.parse("create_clock -name c -period 0 [get_ports clk]")


def test_error_message_contains_filename_and_line():
    with pytest.raises(S.SdcSyntaxError) as ei:
        S.parse("set_input_delay -clock c -max 1 [get_ports {a\n", filename="my.sdc")
    assert "my.sdc" in str(ei.value)
    assert ei.value.line == 1


# ---------------------------------------------------------------------------
# The real constraint file shipped with the project
# ---------------------------------------------------------------------------


def test_real_sdc_file_parses_with_every_required_command(sdc_text):
    d = S.parse(sdc_text, filename="fir_block.sdc")
    s = d.summary()
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
        assert s[cmd] >= 1, f"{cmd} missing from the shipped SDC"


def test_real_sdc_primary_period_is_10ns(sdc_text):
    d = S.parse(sdc_text)
    assert d.primary_period_ns == pytest.approx(10.0)


def test_real_sdc_generated_clock_is_50mhz(sdc_text):
    d = S.parse(sdc_text)
    g = d.generated_clocks[0]
    period = g.period_ns(d.clocks)
    assert period == pytest.approx(20.0)
    assert 1000.0 / period == pytest.approx(50.0)


def test_real_sdc_declares_two_async_clock_groups(sdc_text):
    d = S.parse(sdc_text)
    cg = d.clock_groups[0]
    assert cg.mode == "asynchronous"
    assert len(cg.groups) == 2


def test_parse_file_reports_filename_on_error(tmp_path):
    p = tmp_path / "bad.sdc"
    p.write_text("create_clock -period 1 [get_ports clk\n", encoding="utf-8")
    with pytest.raises(S.SdcSyntaxError) as ei:
        S.parse_file(p)
    assert "bad.sdc" in str(ei.value)
