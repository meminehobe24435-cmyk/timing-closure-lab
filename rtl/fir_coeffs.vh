// fir_coeffs.vh -- 16-tap Hamming-windowed sinc low-pass FIR coefficients.
//
// Generated offline (see tools/gen_coeffs.py) for:
//   N = 16 taps, normalized cutoff fc = 0.22 * fs, Hamming window,
//   quantized to signed Q1.14 (scale 2^14 = 16384) as signed 16-bit.
//
// The impulse response is symmetric (linear phase), which is why the
// coefficient list reads the same forwards and backwards.
//
// Sum of the quantized coefficients = 16386 (ideal 16384), i.e. a DC gain
// error of +1.2e-4 -- an artifact of integer quantization, not a bug.

`ifndef FIR_COEFFS_VH
`define FIR_COEFFS_VH

localparam integer FIR_N_TAPS = 16;

// Signed 16-bit quantized coefficients h[0..15].
localparam signed [15:0] FIR_COEFF_0  = -16'sd45;
localparam signed [15:0] FIR_COEFF_1  =  16'sd41;
localparam signed [15:0] FIR_COEFF_2  =  16'sd214;
localparam signed [15:0] FIR_COEFF_3  = -16'sd29;
localparam signed [15:0] FIR_COEFF_4  = -16'sd871;
localparam signed [15:0] FIR_COEFF_5  = -16'sd497;
localparam signed [15:0] FIR_COEFF_6  =  16'sd2785;
localparam signed [15:0] FIR_COEFF_7  =  16'sd6595;
localparam signed [15:0] FIR_COEFF_8  =  16'sd6595;
localparam signed [15:0] FIR_COEFF_9  =  16'sd2785;
localparam signed [15:0] FIR_COEFF_10 = -16'sd497;
localparam signed [15:0] FIR_COEFF_11 = -16'sd871;
localparam signed [15:0] FIR_COEFF_12 = -16'sd29;
localparam signed [15:0] FIR_COEFF_13 =  16'sd214;
localparam signed [15:0] FIR_COEFF_14 =  16'sd41;
localparam signed [15:0] FIR_COEFF_15 = -16'sd45;

`endif // FIR_COEFFS_VH
