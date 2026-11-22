"""Regression tests for the four Codex review findings on PR #1."""

import os
import unittest

from coverage_agent.exclude.el_parser import parse_exclusion_targets
from coverage_agent.exclude.verify import CONFIRMED, SUSPICIOUS, UNKNOWN_SIGNAL, verify_exclusions
from coverage_agent.model import COVERED, NOT_COVERED, ToggleBin
from coverage_agent.parsers.urg_modinfo import parse_urg_modinfo
from coverage_agent.parsers.urg_text import parse_urg_text
from coverage_agent.report import build_gap_report
from coverage_agent.rtl.scanner import scan_rtl_files

HERE = os.path.dirname(__file__)

_ONE_DIR_EXCLUDED = """Toggle Coverage for Instance : top.u
Port Details
      Toggle Toggle 1->0 Toggle 0->1 Direction
x     No     No          Excluded    INPUT
y     No     Unreachable Unreachable INPUT
-------------------------------------------------------------------------------
"""


class TestPartlyWaivedRow(unittest.TestCase):
    """Finding 1: one direction Excluded, the other No -> still a gap."""

    def test_remaining_direction_is_reported(self):
        bins = parse_urg_modinfo(_ONE_DIR_EXCLUDED).bins
        rep = build_gap_report(bins)
        self.assertEqual([b.signal for b in rep.missing_1_to_0], ["x"])
        self.assertEqual(rep.never_toggled, [])
        self.assertEqual(rep.waived, 1)  # only y is fully waived
        self.assertEqual((rep.transitions_total, rep.transitions_covered), (1, 0))
        x = next(b for b in bins if b.signal == "x")
        self.assertTrue(x.is_gap)
        self.assertFalse(x.waived)


class TestModuleScopedExclusion(unittest.TestCase):
    """Finding 2: a MODULE: entry applies to every instance of the module."""

    def test_module_entry_resolves_against_module_rows(self):
        targets = parse_exclusion_targets('MODULE: foo\nToggle x "logic x"\n')
        self.assertEqual(targets, [("foo", "x", "module")])
        bins = [
            ToggleBin("tb.dut.u0", "x", False, False, NOT_COVERED, NOT_COVERED, module="foo"),
            ToggleBin("tb.dut.u1", "x", True, True, COVERED, COVERED, module="foo"),
        ]
        (r,) = verify_exclusions(targets, bins, scan_rtl_files([]), {})
        self.assertNotEqual(r.verdict, UNKNOWN_SIGNAL)
        self.assertEqual(r.verdict, SUSPICIOUS)  # u0's gap has no RTL justification
        self.assertEqual(r.instance, "module:foo")


class TestWholeSignalCoversAllRows(unittest.TestCase):
    """Finding 3: a whole-signal exclusion is confirmed only if every gap
    row it covers is justified."""

    RTL = """module m (input clk, output logic [1:0] x);
  assign x[0] = 1'b0;
  always_ff @(posedge clk) x[1] <= ~x[1];
endmodule
"""

    def test_one_unjustified_row_makes_it_suspicious(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
            f.write(self.RTL)
            path = f.name
        try:
            rtl = scan_rtl_files([path])
        finally:
            os.unlink(path)
        bins = [
            ToggleBin("i", "x[0]", False, False, NOT_COVERED, NOT_COVERED),
            ToggleBin("i", "x[1]", False, False, NOT_COVERED, NOT_COVERED),
        ]
        rows_alone = verify_exclusions([("i", "x[0]")], bins, rtl, {})
        self.assertEqual(rows_alone[0].verdict, CONFIRMED)  # x[0] really is tied off
        (r,) = verify_exclusions([("i", "x")], bins, rtl, {})
        self.assertEqual(r.verdict, SUSPICIOUS)
        self.assertIn("x[1]", r.detail)


class TestLegacyHeadingNotHijacked(unittest.TestCase):
    """Finding 4: a legacy two-column report with a capitalised heading
    must still go to the legacy parser."""

    def test_capitalised_legacy_heading(self):
        with open(os.path.join(HERE, "..", "samples", "urg_text", "tgl_report.txt")) as f:
            text = f.read()
        capitalised = text.replace("Toggle coverage for instance", "Toggle Coverage for Instance")
        self.assertNotEqual(capitalised, text)
        self.assertEqual(len(parse_urg_text(capitalised)), len(parse_urg_text(text)))
        self.assertEqual(len(parse_urg_text(text)), 23)


if __name__ == "__main__":
    unittest.main()
