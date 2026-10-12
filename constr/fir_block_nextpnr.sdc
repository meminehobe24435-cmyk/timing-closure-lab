# =============================================================================
# constr/fir_block_nextpnr.sdc -- the subset of fir_block.sdc that
# nextpnr-ecp5 0.10-82 can actually consume via --sdc.
# =============================================================================
# MEASURED, not assumed. Each command below was probed against this exact
# nextpnr build; the results were:
#
#   create_clock                 ACCEPTED
#   create_generated_clock       ERROR: Unsupported SDC command
#   set_input_delay              ERROR: Unsupported SDC command
#   set_output_delay             ERROR: Unsupported SDC command
#   set_max_delay                ERROR: Unsupported SDC command
#   set_multicycle_path          ERROR: Unsupported SDC command
#   set_clock_groups             ERROR: Unsupported SDC command
#   set_false_path               ERROR: Expected a single SdcEntity ...
#                                (and "expecting SdcValue argument to -from")
#   create_clock -waveform/-add  ERROR: unsupported argument
#
# So nextpnr's generic SDC front end here honours exactly one command:
# create_clock. Everything else in fir_block.sdc is retained because it is the
# real constraint intent -- it is parsed and enforced by tclab.sdc/tclab.sta
# and would be consumed unchanged by a commercial signoff STA tool -- but it is
# NOT visible to nextpnr.
#
# This split is normal practice (tool-specific constraint subsets); what would
# be dishonest is claiming nextpnr enforced constraints it rejected.
#
# Evidence in the PnR log:
#   Info: constraining clock net 'clk' to 100.00 MHz
# =============================================================================

create_clock -name sys_clk -period 10.000 [get_ports clk]
