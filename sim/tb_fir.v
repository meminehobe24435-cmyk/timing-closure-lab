// tb_fir.v -- self-checking testbench for the FIR block variants.
//
// Selects the DUT with `-D TOP_MODULE=<name>` and `-D PIPE_STAGES=<k>`,
// so the same harness checks the chain, tree and pipelined variants.
//
// The reference model is behavioural: it keeps a history of the input samples
// and computes sum(h[j] * x[t - LAT - j]) combinationally. LAT is the DUT
// input-to-output latency in clocks (1 + PIPE_STAGES), which is exactly the
// property the timing-closure experiments trade away for frequency.
//
// Input stimulus: reset, then a DC step, then full-scale alternating +/-, then
// a pseudo-random sequence from a 32-bit LFSR (so the datapath exercises a wide
// range of operands instead of sitting on one value).

`timescale 1ns / 1ps

`ifndef TOP_MODULE
  `define TOP_MODULE fir_top_chain
`endif
`ifndef PIPE_STAGES
  `define PIPE_STAGES 0
`endif
`ifndef TB_VCD
  `define TB_VCD 0
`endif

`include "fir_coeffs.vh"

module tb_fir;

    localparam integer LAT = 1 + `PIPE_STAGES;

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

    // ---- clock -----------------------------------------------------------
    // 10 ns period = a 100 MHz functional clock. The frequency sweep in the
    // lab is done by nextpnr (static timing), not by shrinking this period.
    initial clk = 1'b0;
    always #5 clk = ~clk;

    // ---- input history: hist[k] = x[t-k] after edge t ---------------------
    reg signed [15:0] hist [0:63];
    integer k;
    initial for (k = 0; k < 64; k = k + 1) hist[k] = 16'sd0;

    always @(posedge clk) begin
        for (k = 63; k > 0; k = k - 1) hist[k] <= hist[k-1];
        hist[0] <= din;
    end

    // ---- reference model -------------------------------------------------
    // After edge t the DUT has dout = sum_j h[j] * x[t - LAT - j],
    // and hist[LAT + j] holds exactly x[t - LAT - j].

    // A plain constant array (not a function) so iverilog and yosys both read
    // it the same way without a forward-declaration headache.
    reg signed [15:0] H [0:15];
    initial begin
        H[0]  = FIR_COEFF_0;  H[1]  = FIR_COEFF_1;
        H[2]  = FIR_COEFF_2;  H[3]  = FIR_COEFF_3;
        H[4]  = FIR_COEFF_4;  H[5]  = FIR_COEFF_5;
        H[6]  = FIR_COEFF_6;  H[7]  = FIR_COEFF_7;
        H[8]  = FIR_COEFF_8;  H[9]  = FIR_COEFF_9;
        H[10] = FIR_COEFF_10; H[11] = FIR_COEFF_11;
        H[12] = FIR_COEFF_12; H[13] = FIR_COEFF_13;
        H[14] = FIR_COEFF_14; H[15] = FIR_COEFF_15;
    end

    integer j;
    reg signed [63:0] ref_sum;
    always @* begin
        ref_sum = 64'sd0;
        for (j = 0; j < 16; j = j + 1)
            ref_sum = ref_sum + ($signed(hist[LAT + j]) * $signed(H[j]));
    end

    // ---- checking --------------------------------------------------------
    integer errors;
    integer checks;
    reg     checking;
    initial begin
        errors   = 0;
        checks   = 0;
        checking = 1'b0;
    end

    always @(negedge clk) begin
        if (checking) begin
            checks = checks + 1;
            if ($signed(dout) !== ref_sum) begin
                if (errors < 10)
                    $display("MISMATCH t=%0t dout=%0d ref=%0d din=%0d LAT=%0d",
                             $time, $signed(dout), ref_sum, $signed(din), LAT);
                errors = errors + 1;
            end
        end
    end

    // ---- stimulus --------------------------------------------------------
    reg [31:0] lfsr;
    integer n;
    initial begin
        if (`TB_VCD) begin
            $dumpfile("sim_out/fir_activity.vcd");
            $dumpvars(0, tb_fir);
        end

        clk      = 1'b0;
        rst_n    = 1'b0;
        din      = 16'sd0;
        lfsr     = 32'hDEADBEEF;
        checking = 1'b0;

        // Every stimulus change lands 1 ns AFTER a rising edge. Driving din at
        // the edge itself races with both the DUT's and this testbench's
        // sampling flops, which makes the two latch different values and drift
        // apart -- the first version of this file did exactly that and produced
        // hundreds of phantom mismatches.
        repeat (10) @(posedge clk);
        #1 rst_n = 1'b1;

        // Give the reference model history time to fill before comparing.
        repeat (LAT + 16) @(posedge clk);
        #1 checking = 1'b1;

        // Phase 1: DC step at mid-scale
        #1 din = 16'sd1024;
        repeat (40) @(posedge clk);

        // Phase 2: alternating full-scale +/- (worst-case toggling)
        for (n = 0; n < 60; n = n + 1) begin
            #1 din = (n % 2) ? -16'sd32767 : 16'sd32767;
            @(posedge clk);
        end

        // Phase 3: pseudo-random operands
        for (n = 0; n < 600; n = n + 1) begin
            lfsr = {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
            #1 din = $signed(lfsr[15:0]);
            @(posedge clk);
        end

        // Phase 4: drain the pipeline, then stop checking
        repeat (20) @(posedge clk);
        #1 checking = 1'b0;
        #1 din      = 16'sd0;
        repeat (5) @(posedge clk);

        if (errors == 0)
            $display("TB_PASS LAT=%0d checks=%0d errors=0", LAT, checks);
        else
            $display("TB_FAIL LAT=%0d checks=%0d errors=%0d", LAT, checks, errors);
        $finish;
    end

    initial begin
        #200000;
        $display("TB_TIMEOUT LAT=%0d", LAT);
        $finish;
    end

endmodule
