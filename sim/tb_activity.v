// tb_activity.v -- activity testbench used for the VCD-based power study.
//
// It applies a pseudo-random input stream to the FIR block and dumps a VCD.
// The *rate* at which the input changes is selectable, which is what makes the
// two power scenarios comparable:
//
//   +mode=full    new random sample every clock   (design running flat out)
//   +mode=gated   new random sample every N clocks (duty-cycled operation,
//                 the effect a clock-enable on the sample register would have
//                 on data-net activity)
//   +mode=idle    constant input                   (no data switching)
//
// Uses named port connections, so the same testbench drives fir_top_chain,
// fir_top_tree, fir_top_pipe or fir_block_top without modification.
//
// Usage: vvp tb_activity.vvp +vcd=<path> +mode=full +cycles=4000 +stride=4

`timescale 1ns / 1ps

`ifndef TOP_MODULE
  `define TOP_MODULE fir_block_top
`endif

module tb_activity;

    reg clk;
    reg rst_n;
    reg signed [15:0] din;
    wire signed [36:0] dout;

    `TOP_MODULE u_dut (
        .clk  (clk),
        .rst_n(rst_n),
        .din  (din),
        .dout (dout)
    );

    // 10 ns period = 100 MHz functional clock.
    initial clk = 1'b0;
    always #5 clk = ~clk;

    reg [31:0] lfsr;
    reg [1023:0] vcd_path;
    reg [255:0] mode;

    integer cycles;
    integer stride;
    integer n;
    integer got;

    function [15:0] next_sample;
        input [15:0] seed_unused;
        begin
            lfsr = {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
            next_sample = lfsr[15:0];
        end
    endfunction

    initial begin
        cycles = 4000;
        stride = 4;
        mode   = "full";
        vcd_path = "sim_out/activity.vcd";

        if (!$value$plusargs("cycles=%d", cycles)) cycles = 4000;
        if (!$value$plusargs("stride=%d", stride)) stride = 4;
        if (!$value$plusargs("mode=%s", mode))     mode = "full";
        got = $value$plusargs("vcd=%s", vcd_path);

        $dumpfile(vcd_path);
        $dumpvars(0, tb_activity);

        lfsr  = 32'h1BADF00D;
        rst_n = 1'b0;
        din   = 16'sd0;

        repeat (10) @(posedge clk);
        #1 rst_n = 1'b1;

        if (mode == "idle") begin
            #1 din = 16'sd1000;                 // constant: no data toggling
            repeat (cycles) @(posedge clk);
        end else if (mode == "gated") begin
            for (n = 0; n < cycles; n = n + 1) begin
                // A new operand only every `stride` clocks; held in between.
                if ((n % stride) == 0) #1 din = next_sample(16'd0);
                @(posedge clk);
            end
        end else begin
            for (n = 0; n < cycles; n = n + 1) begin
                #1 din = next_sample(16'd0);
                @(posedge clk);
            end
        end

        #1 din = 16'sd0;
        repeat (20) @(posedge clk);
        $display("ACTIVITY_DONE mode=%0s cycles=%0d", mode, cycles);
        $finish;
    end

    initial begin
        #5000000;
        $display("ACTIVITY_TIMEOUT");
        $finish;
    end

endmodule
