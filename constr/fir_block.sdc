# =============================================================================
# constr/fir_block.sdc -- timing constraints for the FIR block (LFE5U-45F)
# =============================================================================
# SDC is a TCL dialect. This file is read directly by BOTH:
#   * nextpnr-ecp5 --sdc constr/fir_block.sdc   (the real place-and-route run)
#   * tclab.sdc / tclab.cli                     (our own Python SDC-subset parser)
# so the same source of truth drives the tool and the analysis layer.
#
# Clocking plan
# -------------
#   sys_clk   100.000 MHz  (10.000 ns)  primary, drives the FIR datapath
#   clk_div2   50.000 MHz  (20.000 ns)  generated (/2) by u_div in fir_block_top,
#                                       drives the slow capture/readout register
#
# The two clocks are asynchronous to each other in the sense that matters here:
# clk_div2 is produced by a flop clocked by sys_clk, so its edges have no
# fixed phase relationship that a single-domain analysis can exploit, and the
# crossing into the slow domain is a deliberate, hand-placed CDC point.
# =============================================================================

# --- primary clock -----------------------------------------------------------
create_clock -name sys_clk -period 10.000 [get_ports clk]

# --- generated clock (divide-by-2 from the primary) --------------------------
create_generated_clock -name clk_div2 \
    -source [get_ports clk] \
    -divide_by 2 \
    [get_pins u_div/clk_div2_q]

# --- asynchronous clock groups ----------------------------------------------
# No timing path is analysed between the fast datapath and the slow readout
# domain; the handoff is a captured bus, not a timed path.
set_clock_groups -asynchronous -group {sys_clk} -group {clk_div2}

# --- board-level I/O budget (relative to the primary clock) ------------------
set_input_delay  -clock sys_clk -max 2.000 [get_ports {din[*]}]
set_input_delay  -clock sys_clk -min 0.500 [get_ports {din[*]}]
set_output_delay -clock sys_clk -max 3.000 [get_ports {dout[*]}]
set_output_delay -clock sys_clk -min 0.500 [get_ports {dout[*]}]

# --- asynchronous reset ------------------------------------------------------
# rst_n is an asynchronous input that is only ever de-asserted synchronously
# off-chip, so it must not be timed as data.
set_false_path -from [get_ports rst_n]

# --- multi-cycle hold --------------------------------------------------------
# The externally visible filter output is consumed by a slow consumer that
# needs two sys_clk cycles of valid data before it samples.
set_multicycle_path -setup 2 -from [get_ports {din[*]}] -to [get_ports {dout[*]}]

# --- explicit maximum delay on the I/O-to-I/O bypass path --------------------
# Bounds the input-register -> output-register path independently of the clock
# period, so a period relaxation cannot silently open this path up.
set_max_delay 6.000 -from [get_ports {din[*]}] -to [get_ports {dout[*]}]
