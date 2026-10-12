"""tclab -- a timing-closure experiment lab for digital backend / STA work.

The package drives a real open-source FPGA flow (yosys -> nextpnr-ecp5) and
analyses the artefacts it produces:

    sdc            TCL/SDC constraint parser (subset)
    report_parse   nextpnr JSON report reader
    sta            static timing analysis over the parsed paths
    power          VCD toggle-rate extraction and a relative power model
    lec            yosys-based logical equivalence checking
    flow           end-to-end flow orchestration
    runner         subprocess helper that knows how to find the EDA tools
    cli            command line entry point
"""

__version__ = "1.0.0"

__all__ = [
    "sdc",
    "report_parse",
    "sta",
    "power",
    "lec",
    "flow",
    "runner",
    "cli",
]
