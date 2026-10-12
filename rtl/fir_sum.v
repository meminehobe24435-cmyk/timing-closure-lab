// fir_sum.v -- the FIR summation networks, factored out as standalone modules.
//
// WHY THIS FILE EXISTS
// --------------------
// The timing-closure experiment changes exactly one thing: how the 16 partial
// products are added together. Keeping that network in its own module (with the
// multiplier array outside it) buys two things:
//
//  1. The design is honest about where the timing bottleneck lives. The
//     critical path is entirely inside fir_sum_*; the multipliers are the same
//     in every variant.
//
//  2. It makes logical equivalence checking tractable. A bit-level SAT proof
//     over the whole design has to reason about sixteen 16x16 multipliers at
//     once, which does not converge in reasonable time. Factoring the
//     summation out turns the interesting equivalence obligation into an
//     adder-tree-vs-adder-chain problem over plain adders, which SAT handles
//     immediately. The multiplier array is identical by construction in every
//     variant (same expression, same inputs, same coefficients), so its
//     outputs are legitimate cut points -- this is ordinary compositional LEC,
//     and it is stated as such in the README rather than dressed up.
//
// Both modules take the partial products on a flat packed bus so that the
// interface is portable across yosys and iverilog (unpacked array ports are
// not uniformly supported).

`default_nettype none

// ---------------------------------------------------------------------------
// Serial accumulation chain -- 15 adders in series, depth 15.
// This is the timing-critical baseline.
//
// clk/rst_n are present but unused: the chain is purely combinational. They are
// kept in the port list so that fir_sum_chain and fir_sum_tree are
// pin-compatible, which is what lets yosys `equiv_make` compare them directly.
// ---------------------------------------------------------------------------
module fir_sum_chain #(
    parameter integer W      = 37,
    parameter integer N_TAPS = 16
) (
    input  wire                       clk,
    input  wire                       rst_n,
    input  wire signed [N_TAPS*W-1:0] prod_flat,
    output wire signed [W-1:0]        sum
);
    // Explicitly referenced so lint tools do not flag them as dangling.
    wire unused = clk ^ rst_n;
    wire signed [W-1:0] prod [0:N_TAPS-1];

    genvar g;
    generate
        for (g = 0; g < N_TAPS; g = g + 1) begin : g_unpack
            assign prod[g] = prod_flat[g*W +: W];
        end
    endgenerate

    wire signed [W-1:0] chain [0:N_TAPS-1];
    assign chain[0] = prod[0];

    generate
        for (g = 1; g < N_TAPS; g = g + 1) begin : g_acc
            assign chain[g] = chain[g-1] + prod[g];
        end
    endgenerate

    assign sum = chain[N_TAPS-1];
endmodule

// ---------------------------------------------------------------------------
// Balanced binary adder tree -- 16 -> 8 -> 4 -> 2 -> 1, depth 4.
// PIPE_STAGES optionally cuts the tree with k register stages.
// ---------------------------------------------------------------------------
module fir_sum_tree #(
    parameter integer W            = 37,
    parameter integer N_TAPS       = 16,
    parameter integer PIPE_STAGES  = 0
) (
    input  wire                       clk,
    input  wire                       rst_n,
    input  wire signed [N_TAPS*W-1:0] prod_flat,
    output wire signed [W-1:0]        sum
);
    wire signed [W-1:0] prod [0:N_TAPS-1];

    genvar g;
    generate
        for (g = 0; g < N_TAPS; g = g + 1) begin : g_unpack
            assign prod[g] = prod_flat[g*W +: W];
        end
    endgenerate

    // ---- level 1: 16 -> 8 (registered when PIPE_STAGES >= 1) --------------
    wire signed [W-1:0] n1 [0:7];
    generate
        for (g = 0; g < 8; g = g + 1) begin : g_l1
            if (PIPE_STAGES >= 1) begin : g_reg
                reg signed [W-1:0] r;
                always @(posedge clk or negedge rst_n) begin
                    if (!rst_n) r <= {W{1'b0}};
                    else        r <= prod[2*g] + prod[2*g+1];
                end
                assign n1[g] = r;
            end else begin : g_comb
                assign n1[g] = prod[2*g] + prod[2*g+1];
            end
        end
    endgenerate

    // ---- level 2: 8 -> 4 --------------------------------------------------
    wire signed [W-1:0] n2 [0:3];
    generate
        for (g = 0; g < 4; g = g + 1) begin : g_l2
            if (PIPE_STAGES >= 2) begin : g_reg
                reg signed [W-1:0] r;
                always @(posedge clk or negedge rst_n) begin
                    if (!rst_n) r <= {W{1'b0}};
                    else        r <= n1[2*g] + n1[2*g+1];
                end
                assign n2[g] = r;
            end else begin : g_comb
                assign n2[g] = n1[2*g] + n1[2*g+1];
            end
        end
    endgenerate

    // ---- level 3: 4 -> 2 --------------------------------------------------
    wire signed [W-1:0] n3 [0:1];
    generate
        for (g = 0; g < 2; g = g + 1) begin : g_l3
            if (PIPE_STAGES >= 3) begin : g_reg
                reg signed [W-1:0] r;
                always @(posedge clk or negedge rst_n) begin
                    if (!rst_n) r <= {W{1'b0}};
                    else        r <= n2[2*g] + n2[2*g+1];
                end
                assign n3[g] = r;
            end else begin : g_comb
                assign n3[g] = n2[2*g] + n2[2*g+1];
            end
        end
    endgenerate

    // ---- level 4: 2 -> 1 (never registered; the output flop follows) ------
    assign sum = n3[0] + n3[1];
endmodule

`default_nettype wire
