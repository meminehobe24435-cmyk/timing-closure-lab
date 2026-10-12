// lec_pipe_wrapper.v -- sequential equivalence harness for the retimed FIR.
//
// THE CLAIM BEING PROVEN
// ----------------------
// Not "fir_top_pipe computes the same thing as the reference at every cycle" --
// that is false, because the retimed design has two extra register stages. The
// claim is the one a pipeline actually makes:
//
//     fir_top_pipe.dout(t) == fir_top_tree.dout(t - 2)   for all t
//
// i.e. identical output *sequence*, shifted by the two clocks that the
// pipeline added (PIPE_STAGES = 2 in rtl/fir_top.v).
//
// WHY THE REFERENCE IS fir_top_tree AND NOT fir_top_chain
// ------------------------------------------------------
// fir_top_tree and fir_top_pipe share the *same* balanced adder tree; the only
// difference between them is where the registers sit (retiming). Their
// combinational cones are therefore structurally identical, which is exactly
// what makes the SAT proof tractable.
//
// Comparing against fir_top_chain would fold in a second, independent
// transformation (re-associating a serial accumulation chain into a tree).
// Bit-level CNF-SAT equivalence of two *differently structured* adder networks
// is not tractable -- it is a known hardness result for resolution, and it was
// measured here: the chain-vs-tree miter ran for over 10 minutes on a 14639
// variable / 39944 clause problem with no result. That transformation is
// covered separately by flow/lec_sum_equiv.ys and by exhaustive simulation;
// see the README "LEC" section for the exact scope of each check.
//
// HOW IT IS CHECKED
// -----------------
// The reference output is delayed by exactly LATENCY_SHIFT clocks through a
// two-deep shift register (`d1`, `d2`), and that delayed reference is compared
// against the pipelined output every cycle. `mismatch` is registered so it is
// a plain 1-bit observable, and an `assert` states the obligation.
//
// Both designs are reset together and both start from all-zero state, so the
// shift relation holds from cycle 0 -- there is no "wait for the pipeline to
// fill" caveat to argue about, and the assertion does not need a valid guard.
//
// This harness is *only* used for formal checking; it is not part of the
// synthesised design.

`default_nettype none

module lec_pipe_wrapper #(
    parameter integer LATENCY_SHIFT = 2
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire signed [15:0]   din,
    output wire                 mismatch
);
    wire signed [36:0] base_out;
    wire signed [36:0] pipe_out;

    fir_top_tree u_base (
        .clk(clk), .rst_n(rst_n), .din(din), .dout(base_out)
    );

    fir_top_pipe u_pipe (
        .clk(clk), .rst_n(rst_n), .din(din), .dout(pipe_out)
    );

    // Delay line of LATENCY_SHIFT stages on the reference output.
    reg signed [36:0] d1, d2;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            d1 <= 37'sd0;
            d2 <= 37'sd0;
        end else begin
            d1 <= base_out;
            d2 <= d1;
        end
    end

    reg mismatch_q;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) mismatch_q <= 1'b0;
        else        mismatch_q <= (d2 !== pipe_out);
    end

    assign mismatch = mismatch_q;

    // The obligation, as an assert yosys `sat -prove-asserts` can consume.
    always @(posedge clk) begin
        if (rst_n) assert (mismatch_q == 1'b0);
    end

endmodule

`default_nettype wire
