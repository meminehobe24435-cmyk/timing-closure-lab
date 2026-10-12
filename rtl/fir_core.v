// fir_core.v -- parameterized 16-tap direct-form FIR filter block.
//
// This is the DUT for the timing-closure experiments in this lab. It is a
// deliberately "realistic but small" block-level datapath: a 16-tap FIR with
// signed 16x16 multipliers followed by a wide (37-bit) accumulator.
//
// Structure, and why it is split this way:
//
//   sample delay line (sr)     16 x 16-bit shift register
//   multiplier array (prod)    16 constant multipliers, sr[g] * h[g]
//   summation network          fir_sum_chain OR fir_sum_tree   <-- the knob
//   output register (dout)
//
// The whole point of the design is that the *summation network* is what the
// timing-closure experiments change, while the arithmetic function stays
// identical. Two parameters do that, and they are passed straight through to
// rtl/fir_sum.v:
//
//   SUM_STYLE   = 0  -> serial accumulation chain  (longest combinational path)
//               = 1  -> balanced binary adder tree (log2 depth)
//
//   PIPE_STAGES = k  -> insert k register cuts inside the summation tree
//                       (only meaningful with SUM_STYLE = 1)
//
// Latency (in clocks, input sample -> dout):
//   SUM_STYLE=0                 : 1
//   SUM_STYLE=1, PIPE_STAGES=k  : 1 + k
//
// Interface convention: free-running, one new input sample per clock. There is
// no valid/ready handshake and no backpressure -- the block is meant to sit on
// a continuous sample stream. This keeps the sequential-equivalence proof in
// the LEC step tractable and is stated explicitly in the README.

`include "fir_coeffs.vh"

module fir_core #(
    parameter integer DATA_W      = 16,
    parameter integer COEF_W      = 16,
    parameter integer N_TAPS      = FIR_N_TAPS,
    parameter integer SUM_STYLE   = 0,
    parameter integer PIPE_STAGES = 0,
    // Accumulator width: full product width plus log2(N) growth plus sign.
    parameter integer ACC_W       = DATA_W + COEF_W + 4 + 1
) (
    input  wire                       clk,
    input  wire                       rst_n,
    input  wire signed [DATA_W-1:0]   din,
    output reg  signed [ACC_W-1:0]    dout
);

    // ---------------------------------------------------------------------
    // Coefficient ROM (compile-time constants -> constant multipliers)
    // ---------------------------------------------------------------------
    wire signed [COEF_W-1:0] coeff [0:N_TAPS-1];
    assign coeff[0]  = FIR_COEFF_0;
    assign coeff[1]  = FIR_COEFF_1;
    assign coeff[2]  = FIR_COEFF_2;
    assign coeff[3]  = FIR_COEFF_3;
    assign coeff[4]  = FIR_COEFF_4;
    assign coeff[5]  = FIR_COEFF_5;
    assign coeff[6]  = FIR_COEFF_6;
    assign coeff[7]  = FIR_COEFF_7;
    assign coeff[8]  = FIR_COEFF_8;
    assign coeff[9]  = FIR_COEFF_9;
    assign coeff[10] = FIR_COEFF_10;
    assign coeff[11] = FIR_COEFF_11;
    assign coeff[12] = FIR_COEFF_12;
    assign coeff[13] = FIR_COEFF_13;
    assign coeff[14] = FIR_COEFF_14;
    assign coeff[15] = FIR_COEFF_15;

    // ---------------------------------------------------------------------
    // Sample shift register (the FIR delay line)
    // ---------------------------------------------------------------------
    reg signed [DATA_W-1:0] sr [0:N_TAPS-1];
    integer i;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < N_TAPS; i = i + 1)
                sr[i] <= {DATA_W{1'b0}};
        end else begin
            for (i = N_TAPS - 1; i > 0; i = i - 1)
                sr[i] <= sr[i-1];
            sr[0] <= din;
        end
    end

    // ---------------------------------------------------------------------
    // Multiplier array -> flat packed product bus
    // ---------------------------------------------------------------------
    wire signed [ACC_W-1:0] prod [0:N_TAPS-1];
    wire signed [N_TAPS*ACC_W-1:0] prod_flat;

    genvar g;
    generate
        for (g = 0; g < N_TAPS; g = g + 1) begin : g_mult
            assign prod[g] = sr[g] * coeff[g];
            assign prod_flat[g*ACC_W +: ACC_W] = prod[g];
        end
    endgenerate

    // ---------------------------------------------------------------------
    // Summation network
    // ---------------------------------------------------------------------
    wire signed [ACC_W-1:0] sum_pre;

    generate
    if (SUM_STYLE == 0) begin : g_chain
        fir_sum_chain #(.W(ACC_W), .N_TAPS(N_TAPS)) u_sum (
            .clk      (clk),
            .rst_n    (rst_n),
            .prod_flat(prod_flat),
            .sum      (sum_pre)
        );
    end else begin : g_tree
        fir_sum_tree #(.W(ACC_W), .N_TAPS(N_TAPS), .PIPE_STAGES(PIPE_STAGES)) u_sum (
            .clk      (clk),
            .rst_n    (rst_n),
            .prod_flat(prod_flat),
            .sum      (sum_pre)
        );
    end
    endgenerate

    // ---------------------------------------------------------------------
    // Output register
    // ---------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) dout <= {ACC_W{1'b0}};
        else        dout <= sum_pre;
    end

endmodule

`default_nettype wire
