"""Regression test for a real bug found while scanning a multi-IP SoC
(pulp-platform's apb_uart_sv.sv declares TX_FIFO_DEPTH=16,
uart_interrupt.sv — a different file in the same IP — declares
TX_FIFO_DEPTH=32). Scanning multiple RTL files together must resolve
each tie-off/gate against its OWN file's declared defaults, never a
flat last-file-wins merge across every scanned file."""

import os
import tempfile
import unittest

from coverage_agent.exclude.candidates import SUGGESTED_EXCLUDE, suggest_excludes
from coverage_agent.model import ToggleBin
from coverage_agent.rtl.scanner import scan_rtl_files

FILE_A = """
module a;
  parameter TX_FIFO_DEPTH = 16;
  // reserved bits above the (small) real depth are tied off in file A
  wire [31:0] status_a;
  assign status_a[31:TX_FIFO_DEPTH] = {(32-TX_FIFO_DEPTH){1'b0}};
endmodule
"""

FILE_B = """
module b;
  parameter TX_FIFO_DEPTH = 32;
  // same param name, different value — file B's own reserved range
  wire [63:0] status_b;
  assign status_b[63:TX_FIFO_DEPTH] = {(64-TX_FIFO_DEPTH){1'b0}};
endmodule
"""


class TestParamCollisionAcrossFiles(unittest.TestCase):
    def test_params_by_file_kept_separate(self):
        with tempfile.TemporaryDirectory() as d:
            pa, pb = os.path.join(d, "a.v"), os.path.join(d, "b.v")
            with open(pa, "w") as f:
                f.write(FILE_A)
            with open(pb, "w") as f:
                f.write(FILE_B)
            facts = scan_rtl_files([pa, pb])

            self.assertEqual(facts.params_by_file[pa]["TX_FIFO_DEPTH"], 16)
            self.assertEqual(facts.params_by_file[pb]["TX_FIFO_DEPTH"], 32)
            # the flat merged view is last-file-wins (32) — informational only
            self.assertEqual(facts.params["TX_FIFO_DEPTH"], 32)

    def test_reserved_range_resolves_correctly_per_file_not_globally(self):
        """This is the actual failure mode: without per-file scoping,
        file A's reserved range would incorrectly use file B's
        TX_FIFO_DEPTH=32 (since B is scanned after A), making bit 20
        (which IS reserved in A, since A's real depth is 16) look like
        it's NOT reserved — a real signal would then fall through to
        unexplained_gap instead of being correctly identified as a
        tie-off, or worse, in other shapes, produce a wrong verdict."""
        with tempfile.TemporaryDirectory() as d:
            pa, pb = os.path.join(d, "a.v"), os.path.join(d, "b.v")
            with open(pa, "w") as f:
                f.write(FILE_A)
            with open(pb, "w") as f:
                f.write(FILE_B)
            facts = scan_rtl_files([pa, pb])

            bins = [
                ToggleBin(instance="i", signal="status_a[20]", hit_0_to_1=False, hit_1_to_0=False),
            ]
            # config supplies no override — must fall back to file A's
            # OWN declared default (16), not file B's (32) or the
            # flat merged value.
            candidates = {c.signal: c for c in suggest_excludes(bins, facts, {})}
            self.assertEqual(candidates["status_a[20]"].disposition, SUGGESTED_EXCLUDE)


if __name__ == "__main__":
    unittest.main()
