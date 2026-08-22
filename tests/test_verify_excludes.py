import os
import unittest

from coverage_agent.exclude.el_parser import parse_ucm_exclusion_file
from coverage_agent.exclude.verify import (
    CONFIRMED,
    REDUNDANT,
    SUSPICIOUS,
    UNKNOWN_SIGNAL,
    verify_exclusions,
)
from coverage_agent.parsers.urg_text import parse_urg_text_file
from coverage_agent.rtl.scanner import scan_rtl_files

BASE = os.path.dirname(__file__)
RTL_V1 = os.path.join(BASE, "..", "soc_sample", "rtl", "apb_gpio.v")
TGL_V1 = os.path.join(BASE, "..", "samples", "urg_text", "apb_gpio_tgl.txt")


def _by_signal(results):
    return {r.signal: r for r in results}


class TestElParser(unittest.TestCase):
    def test_parses_instance_and_signal_pairs(self):
        text = (
            'INSTANCE: tb_top.dut.u_apb_gpio\n'
            'Exclude Toggle "pslverr" -detail "01" -comment "tie-off"\n'
            'Exclude Toggle "irq" -detail "01" -comment "not tested"\n'
        )
        pairs = parse_ucm_exclusion_file(text)
        self.assertEqual(pairs, [
            ("tb_top.dut.u_apb_gpio", "pslverr"),
            ("tb_top.dut.u_apb_gpio", "irq"),
        ])

    def test_multiple_instances(self):
        text = (
            "INSTANCE: a\n"
            'Exclude Toggle "x" -detail "01" -comment "c"\n'
            "INSTANCE: b\n"
            'Exclude Toggle "y" -detail "01" -comment "c"\n'
        )
        self.assertEqual(parse_ucm_exclusion_file(text), [("a", "x"), ("b", "y")])


class TestVerifyExclusions(unittest.TestCase):
    def setUp(self):
        self.bins = parse_urg_text_file(TGL_V1)
        self.rtl = scan_rtl_files([RTL_V1])
        self.params = {"NUM_GPIO": 8, "ENABLE_IRQ": 1}
        self.instance = "tb_top.dut.u_apb_gpio"

    def test_valid_tieoff_exclusion_is_confirmed(self):
        results = verify_exclusions([(self.instance, "pslverr")], self.bins, self.rtl, self.params)
        self.assertEqual(results[0].verdict, CONFIRMED)

    def test_wrongly_excluded_live_signal_is_suspicious(self):
        """The exact 'owner added it incorrectly' scenario: irq is live
        logic under ENABLE_IRQ=1 (base_project), but someone excluded
        it anyway (e.g. thinking interrupts 'aren't tested here')."""
        results = verify_exclusions([(self.instance, "irq")], self.bins, self.rtl, self.params)
        self.assertEqual(results[0].verdict, SUSPICIOUS)

    def test_wrongly_excluded_untested_signal_is_suspicious(self):
        results = verify_exclusions([(self.instance, "gpio_out[3]")], self.bins, self.rtl, self.params)
        self.assertEqual(results[0].verdict, SUSPICIOUS)

    def test_typo_signal_is_unknown(self):
        results = verify_exclusions([(self.instance, "pslverrx")], self.bins, self.rtl, self.params)
        self.assertEqual(results[0].verdict, UNKNOWN_SIGNAL)

    def test_excluding_a_fully_covered_signal_is_redundant(self):
        results = verify_exclusions([(self.instance, "pclk")], self.bins, self.rtl, self.params)
        self.assertEqual(results[0].verdict, REDUNDANT)

    def test_same_signal_different_derivative_flips_verdict(self):
        """Mirrors the derivative-awareness guarantee from suggest_excludes:
        excluding irq is SUSPICIOUS under base_project (ENABLE_IRQ=1) but
        CONFIRMED under deriv_lowpower (ENABLE_IRQ=0)."""
        base = verify_exclusions([(self.instance, "irq")], self.bins, self.rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 1})
        lowpower = verify_exclusions([(self.instance, "irq")], self.bins, self.rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 0})
        self.assertEqual(base[0].verdict, SUSPICIOUS)
        self.assertEqual(lowpower[0].verdict, CONFIRMED)


if __name__ == "__main__":
    unittest.main()
