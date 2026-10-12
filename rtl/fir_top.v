// fir_top.v -- three interchangeable top-level wrappers around fir_core.
//
// All three expose an identical port list so that the same SDC constraint
// file, the same testbench harness and the same equivalence-check script can
// be pointed at any of them:
//
//   fir_top_chain : SUM_STYLE=0, PIPE_STAGES=0   latency 1   (baseline)
//   fir_top_tree  : SUM_STYLE=1, PIPE_STAGES=0   latency 1   (re-associated)
//   fir_top_pipe  : SUM_STYLE=1, PIPE_STAGES=2   latency 3   (retimed/pipelined)
//
// `fir_top_chain` vs `fir_top_tree` are *zero-latency* equivalent: the only
// difference is the association order of the additions. That makes them a
// clean target for yosys `equiv_make`/`equiv_simple`.
//
// `fir_top_pipe` is equivalent to the baseline modulo a 2-cycle output delay,
// which is what the miter/`sat` sequential proof in tclab/lec.py establishes.

`include "fir_coeffs.vh"

`ifndef FIR_ACC_W
`define FIR_ACC_W 37
`endif

// ---------------------------------------------------------------------------
// Baseline: serial accumulation chain, combinational critical path
// ---------------------------------------------------------------------------
module fir_top_chain (
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire signed [15:0]      din,
    output wire signed [`FIR_ACC_W-1:0] dout
);
    fir_core #(
        .DATA_W(16), .COEF_W(16), .N_TAPS(FIR_N_TAPS),
        .SUM_STYLE(0), .PIPE_STAGES(0), .ACC_W(`FIR_ACC_W)
    ) u_core (
        .clk(clk), .rst_n(rst_n), .din(din), .dout(dout)
    );
endmodule

// ---------------------------------------------------------------------------
// Re-associated: balanced adder tree, same latency, shorter logic depth
// ---------------------------------------------------------------------------
module fir_top_tree (
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire signed [15:0]      din,
    output wire signed [`FIR_ACC_W-1:0] dout
);
    fir_core #(
        .DATA_W(16), .COEF_W(16), .N_TAPS(FIR_N_TAPS),
        .SUM_STYLE(1), .PIPE_STAGES(0), .ACC_W(`FIR_ACC_W)
    ) u_core (
        .clk(clk), .rst_n(rst_n), .din(din), .dout(dout)
    );
endmodule

// ---------------------------------------------------------------------------
// Retimed: adder tree with 2 register cuts (+2 clocks of latency)
// ---------------------------------------------------------------------------
module fir_top_pipe (
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire signed [15:0]      din,
    output wire signed [`FIR_ACC_W-1:0] dout
);
    fir_core #(
        .DATA_W(16), .COEF_W(16), .N_TAPS(FIR_N_TAPS),
        .SUM_STYLE(1), .PIPE_STAGES(2), .ACC_W(`FIR_ACC_W)
    ) u_core (
        .clk(clk), .rst_n(rst_n), .din(din), .dout(dout)
    );
endmodule

`default_nettype wire
