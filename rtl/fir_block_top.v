// fir_block_top.v -- block-level top for the timing-closure lab.
//
// This wraps fir_core with the clocking structure that the SDC file actually
// constrains, so that every command in constr/fir_block.sdc refers to a real
// object in the netlist rather than to a decorative name:
//
//   clk        -> u_core            : primary domain, 100 MHz (sys_clk)
//   clk_div2   -> u_slow_capture    : generated /2 domain (clk_div2)
//
// u_div is a plain toggle flop; its output IS the generated clock. That makes
// `create_generated_clock -divide_by 2` a statement about real silicon, and it
// makes `set_clock_groups -asynchronous` meaningful: the fast FIR output
// crosses into the slow capture register and that crossing is deliberately
// excluded from single-domain timing analysis.
//
// The crossing is a captured bus (no handshake, no synchroniser). It is a real
// structural CDC and is called out as such in the README's limitations: this
// lab does not implement or verify a CDC synchroniser.

`ifndef FIR_ACC_W
`define FIR_ACC_W 37
`endif

// ---------------------------------------------------------------------------
// Divide-by-2 clock generator. Instanced as u_div so that the SDC can address
// the flop directly with [get_pins u_div/clk_div2_q].
// ---------------------------------------------------------------------------
module clk_div2_gen (
    input  wire clk,
    input  wire rst_n,
    output reg  clk_div2_q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) clk_div2_q <= 1'b0;
        else        clk_div2_q <= ~clk_div2_q;
    end
endmodule

// ---------------------------------------------------------------------------
// Block top
// ---------------------------------------------------------------------------
module fir_block_top #(
    parameter integer SUM_STYLE   = 0,
    parameter integer PIPE_STAGES = 0
) (
    input  wire                          clk,        // sys_clk, 100 MHz
    input  wire                          rst_n,
    input  wire signed [15:0]            din,
    output wire signed [`FIR_ACC_W-1:0]  dout,       // fast-domain FIR output
    output wire                          clk_div2,   // generated clock, 50 MHz
    output reg  signed [`FIR_ACC_W-1:0]  dout_slow   // slow-domain capture
);

    // ---- generated /2 clock -------------------------------------------------
    wire clk_div2_q;

    clk_div2_gen u_div (
        .clk       (clk),
        .rst_n     (rst_n),
        .clk_div2_q(clk_div2_q)
    );

    assign clk_div2 = clk_div2_q;

    // ---- the FIR datapath (primary domain) ---------------------------------
    wire signed [`FIR_ACC_W-1:0] fir_out;

    fir_core #(
        .DATA_W(16), .COEF_W(16), .N_TAPS(16),
        .SUM_STYLE(SUM_STYLE), .PIPE_STAGES(PIPE_STAGES),
        .ACC_W(`FIR_ACC_W)
    ) u_core (
        .clk(clk), .rst_n(rst_n), .din(din), .dout(fir_out)
    );

    assign dout = fir_out;

    // ---- slow-domain capture (generated clock domain) ----------------------
    always @(posedge clk_div2_q or negedge rst_n) begin
        if (!rst_n) dout_slow <= {`FIR_ACC_W{1'b0}};
        else        dout_slow <= fir_out;
    end

endmodule

`default_nettype wire
