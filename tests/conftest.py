"""Shared pytest fixtures for the tclab test suite.

The suite is split by dependency:

* most tests are pure Python (parsers, STA arithmetic, the power model) and run
  anywhere, including CI with no EDA tools installed;
* a small number are marked ``eda`` and are skipped automatically when yosys /
  nextpnr / iverilog are not reachable.

Real tool output is used as fixed input wherever a parser is under test
(``samples/``), so the parsing tests are grounded in bytes the tools actually
produced rather than in hand-written approximations of them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "samples"
REPORTS = ROOT / "reports"


def _eda_available() -> bool:
    """True only when every EDA tool this lab needs actually runs.

    Delegates to ``tclab.runner.tools_available``, which decides availability by
    what the tool printed rather than by its exit code. That distinction is
    load-bearing: a missing binary makes the shell print "is not recognized as
    an internal or external command" and return non-zero, and an earlier version
    of this probe accepted that string as a version banner -- so the EDA tests
    *ran* on a CI runner with no toolchain and failed instead of skipping.
    """
    try:
        from tclab.runner import ToolUnavailable, tools_available

        return tools_available()
    except (ToolUnavailable, OSError, ImportError):
        return False


EDA_AVAILABLE = _eda_available()

requires_eda = pytest.mark.skipif(
    not EDA_AVAILABLE, reason="EDA tools (yosys/nextpnr-ecp5/iverilog) not available"
)


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    return SAMPLES


@pytest.fixture(scope="session")
def small_report() -> dict:
    """A small, real nextpnr report committed under samples/."""
    p = SAMPLES / "nextpnr_report_sample.json"
    if not p.exists():
        pytest.skip(f"sample report missing: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def full_report() -> dict:
    p = SAMPLES / "nextpnr_report_full.json"
    if not p.exists():
        pytest.skip(f"sample report missing: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sample_vcd() -> str:
    p = SAMPLES / "activity_sample.vcd"
    if not p.exists():
        pytest.skip(f"sample VCD missing: {p}")
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def sdc_text() -> str:
    p = ROOT / "constr" / "fir_block.sdc"
    return p.read_text(encoding="utf-8")
