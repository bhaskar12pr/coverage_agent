"""Validates the RTL scanner against real, external RTL — not a
synthetic sample built to fit the scanner's patterns. See
soc_sample/external/pulp_apb_gpio/ATTRIBUTION.md for provenance
(pulp-platform/apb_gpio, Solderpad HL v0.51)."""

import os
import unittest

from coverage_agent.exclude.candidates import SUGGESTED_EXCLUDE, UNEXPLAINED_GAP, suggest_excludes
from coverage_agent.parsers.urg_text import parse_urg_text_file
from coverage_agent.rtl.scanner import scan_rtl_files

BASE = os.path.dirname(__file__)
RTL = os.path.join(BASE, "..", "soc_sample", "external", "pulp_apb_gpio", "apb_gpio.sv")
TGL = os.path.join(BASE, "..", "samples", "urg_text", "pulp_apb_gpio_tgl.txt")
PARAMS = {"APB_ADDR_WIDTH": 12, "PAD_NUM": 32, "NBIT_PADCFG": 4}


class TestRealPulpGpioRtl(unittest.TestCase):
    def test_scanner_finds_the_two_real_tieoffs(self):
        facts = scan_rtl_files([RTL])
        names = {t.signal for t in facts.tie_offs}
        self.assertEqual(names, {"PREADY", "PSLVERR"})

    def test_scanner_finds_no_generate_gates(self):
        """This RTL gates its upper register bank via runtime
        `if (i < PAD_NUM)` loop guards, not `generate if/else` — a
        pattern the scanner doesn't parse yet. Documenting that as a
        passing assertion (not just a comment) so a future scanner
        change that starts finding something here is a deliberate,
        reviewed change, not a silent behavior shift."""
        facts = scan_rtl_files([RTL])
        self.assertEqual(facts.generate_gates, [])

    def test_real_params_extracted(self):
        facts = scan_rtl_files([RTL])
        self.assertEqual(facts.params, PARAMS)

    def test_gap_classification_against_real_toggle_report(self):
        bins = parse_urg_text_file(TGL)
        facts = scan_rtl_files([RTL])
        candidates = {c.signal: c for c in suggest_excludes(bins, facts, PARAMS)}

        self.assertEqual(candidates["PREADY"].disposition, SUGGESTED_EXCLUDE)
        self.assertEqual(candidates["PSLVERR"].disposition, SUGGESTED_EXCLUDE)
        # Real, live signals with no matching pattern — must NOT be
        # excluded just because they're real RTL; safe default holds.
        self.assertEqual(candidates["interrupt"].disposition, UNEXPLAINED_GAP)
        self.assertEqual(candidates["gpio_out[5]"].disposition, UNEXPLAINED_GAP)


if __name__ == "__main__":
    unittest.main()
