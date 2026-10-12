"""Build the committed ``samples/`` fixtures from real tool output.

CI does not install the EDA toolchain, so the parser and analysis tests need
real artefacts checked into the repository. This script produces them from a
previous local run -- it filters and trims, it never fabricates.

    py -3.12 tools/make_samples.py

Inputs (produced by ``py -3.12 -m tclab run``):
    reports/baseline_report.json     full nextpnr report
    sim_out/activity_full.vcd        full activity dump (large)

Outputs:
    samples/nextpnr_report_full.json  the baseline report, trimmed lists only
    samples/nextpnr_report_sample.json a smaller trim, for fast tests
    samples/activity_sample.vcd       the first N lines of a real VCD
    samples/README.md                 provenance for every file

One and only one rewrite is applied to the report: the absolute path of the
local OSS CAD Suite installation inside the ``sources`` fields is reduced to
``share/yosys/...``. Those strings are the *tool's* own internal paths
(``D:\\OSS-CA~1\\bin\\../share/yosys/lattice/...``); leaving them in would bake
this machine's drive layout into the repository and would make the sample
behave differently on Linux CI. Nothing else is altered -- all delays,
utilisation numbers and cell names are exactly as the tool printed them.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
REPORTS = ROOT / "reports"

#: How many entries of each long list to keep in the trimmed report.
PATH_KEEP = 3
NET_KEEP = 60
VCD_LINES = 4000

#: Rewrites an absolute OSS CAD Suite path to one relative to its share/ dir.
_OSS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/].*?[\\/](share[\\/].*)$")


def _normalise_sources(obj: object) -> int:
    """Reduce absolute oss-cad-suite paths in ``sources`` fields. Returns count."""
    changed = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "sources" and isinstance(value, list):
                new = []
                for item in value:
                    if isinstance(item, str):
                        m = _OSS_PATH_RE.match(item)
                        if m:
                            item = m.group(1).replace("\\", "/")
                            changed += 1
                    new.append(item)
                obj[key] = new
            else:
                changed += _normalise_sources(value)
    elif isinstance(obj, list):
        for item in obj:
            changed += _normalise_sources(item)
    return changed


def _trim(report: dict) -> dict:
    out = dict(report)
    if isinstance(out.get("critical_paths"), list):
        out["critical_paths"] = out["critical_paths"][:PATH_KEEP]
    if isinstance(out.get("detailed_net_timings"), list):
        out["detailed_net_timings"] = out["detailed_net_timings"][:NET_KEEP]
    return out


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)

    full_src = REPORTS / "baseline_report.json"
    if not full_src.exists():
        print(f"missing {full_src}; run `py -3.12 -m tclab run` first", file=sys.stderr)
        return 1
    full = json.loads(full_src.read_text(encoding="utf-8"))
    rewritten = _normalise_sources(full)

    (SAMPLES / "nextpnr_report_full.json").write_text(
        json.dumps(full, indent=1), encoding="utf-8"
    )
    (SAMPLES / "nextpnr_report_sample.json").write_text(
        json.dumps(_trim(full), indent=1), encoding="utf-8"
    )

    vcd_src = ROOT / "sim_out" / "activity_full.vcd"
    if vcd_src.exists():
        lines = vcd_src.read_text(encoding="utf-8", errors="replace").splitlines()
        head = lines[:VCD_LINES]
        (SAMPLES / "activity_sample.vcd").write_text("\n".join(head) + "\n", encoding="utf-8")

    fmax = full.get("fmax", {})
    util = full.get("utilization", {})
    prov = [
        "# samples/",
        "",
        "Fixed inputs for the test suite and for CI, where the EDA toolchain is not",
        "installed. **These are real tool outputs, trimmed -- nothing here is",
        "hand-written or synthesised.**",
        "",
        "Regenerate with `py -3.12 tools/make_samples.py` after a local",
        "`py -3.12 -m tclab run`.",
        "",
        "## Provenance",
        "",
        "- `nextpnr_report_full.json` -- `nextpnr-ecp5 --report` for the **baseline**",
        "  variant (serial accumulation chain).",
        "- `nextpnr_report_sample.json` -- the same report with `critical_paths`",
        f"  truncated to {PATH_KEEP} entries and `detailed_net_timings` to {NET_KEEP},",
        "  to keep the committed file small.",
        "- `activity_sample.vcd` -- the first "
        f"{VCD_LINES} lines of a real iverilog `$dumpvars` dump.",
        "",
        "## The one rewrite applied",
        "",
        f"{rewritten} `sources` entries that pointed at the local OSS CAD Suite",
        "installation were reduced to a path relative to its `share/` directory, e.g.",
        "",
        "```",
        r"D:\OSS-CA~1\bin\../share/yosys/lattice/cells_map_trellis.v:108.23-108.24",
        "  ->  share/yosys/lattice/cells_map_trellis.v:108.23-108.24",
        "```",
        "",
        "Those strings are the toolchain's own internal file paths. Keeping them",
        "absolute would bake one machine's drive layout into the repository and make",
        "the sample behave differently on Linux CI. **No timing number, utilisation",
        "figure, cell name or net name was altered.**",
        "",
        "## What the baseline report says",
        "",
    ]
    for clock, ent in fmax.items():
        prov.append(
            f"- clock `{clock}`: achieved {ent.get('achieved')} MHz against a "
            f"{ent.get('constraint')} MHz constraint"
        )
    for cell in ("TRELLIS_COMB", "TRELLIS_FF", "MULT18X18D", "DP16KD"):
        ent = util.get(cell)
        if ent:
            prov.append(f"- {cell}: {ent.get('used')} / {ent.get('available')}")
    prov.append("")
    (SAMPLES / "README.md").write_text("\n".join(prov), encoding="utf-8")

    for f in sorted(SAMPLES.iterdir()):
        print(f"{f.stat().st_size:>10,}  {f.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
