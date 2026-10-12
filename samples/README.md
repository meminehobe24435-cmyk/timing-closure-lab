# samples/

Fixed inputs for the test suite and for CI, where the EDA toolchain is not
installed. **These are real tool outputs, trimmed -- nothing here is
hand-written or synthesised.**

Regenerate with `py -3.12 tools/make_samples.py` after a local
`py -3.12 -m tclab run`.

## Provenance

- `nextpnr_report_full.json` -- `nextpnr-ecp5 --report` for the **baseline**
  variant (serial accumulation chain).
- `nextpnr_report_sample.json` -- the same report with `critical_paths`
  truncated to 3 entries and `detailed_net_timings` to 60,
  to keep the committed file small.
- `activity_sample.vcd` -- the first 4000 lines of a real iverilog `$dumpvars` dump.

## The one rewrite applied

34 `sources` entries that pointed at the local OSS CAD Suite
installation were reduced to a path relative to its `share/` directory, e.g.

```
D:\OSS-CA~1\bin\../share/yosys/lattice/cells_map_trellis.v:108.23-108.24
  ->  share/yosys/lattice/cells_map_trellis.v:108.23-108.24
```

Those strings are the toolchain's own internal file paths. Keeping them
absolute would bake one machine's drive layout into the repository and make
the sample behave differently on Linux CI. **No timing number, utilisation
figure, cell name or net name was altered.**

## What the baseline report says

- clock `$glbnet$clk$TRELLIS_IO_IN`: achieved 50.97100067138672 MHz against a 100 MHz constraint
- TRELLIS_COMB: 1132 / 43848
- TRELLIS_FF: 331 / 43848
- MULT18X18D: 16 / 72
- DP16KD: 0 / 108
