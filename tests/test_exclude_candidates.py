import os
import unittest

from coverage_agent.exclude.candidates import SUGGESTED_EXCLUDE, UNEXPLAINED_GAP, suggest_excludes
from coverage_agent.exclude.formatter import format_ucm_exclusions
from coverage_agent.parsers.urg_text import parse_urg_text_file
from coverage_agent.rtl.scanner import scan_rtl_files

BASE = os.path.dirname(__file__)
RTL_V1 = os.path.join(BASE, "..", "soc_sample", "rtl", "apb_gpio.v")
RTL_V2 = os.path.join(BASE, "..", "soc_sample", "rtl", "apb_gpio_v2.v")
TGL_V1 = os.path.join(BASE, "..", "samples", "urg_text", "apb_gpio_tgl.txt")
TGL_V2 = os.path.join(BASE, "..", "samples", "urg_text", "apb_gpio_v2_tgl.txt")


def _by_signal(candidates):
    return {c.signal: c for c in candidates}


class TestExcludeCandidates(unittest.TestCase):
    def test_derivative_awareness_irq(self):
        """Same RTL, same coverage gap, different config -> different
        verdict. This is the core requirement: don't blindly waive a
        signal without checking whether THIS config's logic is live."""
        bins = parse_urg_text_file(TGL_V1)
        rtl = scan_rtl_files([RTL_V1])

        base = _by_signal(suggest_excludes(bins, rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 1}))
        self.assertEqual(base["irq"].disposition, UNEXPLAINED_GAP)

        lowpower = _by_signal(suggest_excludes(bins, rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 0}))
        self.assertEqual(lowpower["irq"].disposition, SUGGESTED_EXCLUDE)

    def test_ip_version_awareness_pslverr(self):
        """v1 ties off pslverr; v2 adds real logic for it. An exclusion
        learned from v1 must not silently apply to v2."""
        v1_bins = parse_urg_text_file(TGL_V1)
        v1_rtl = scan_rtl_files([RTL_V1])
        v1_candidates = _by_signal(suggest_excludes(v1_bins, v1_rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 1}))
        self.assertEqual(v1_candidates["pslverr"].disposition, SUGGESTED_EXCLUDE)

        v2_bins = parse_urg_text_file(TGL_V2)
        v2_rtl = scan_rtl_files([RTL_V2])
        v2_candidates = _by_signal(suggest_excludes(v2_bins, v2_rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 1}))
        self.assertEqual(v2_candidates["pslverr"].disposition, UNEXPLAINED_GAP)

    def test_reserved_bits_scale_with_num_gpio(self):
        bins = parse_urg_text_file(TGL_V1)
        rtl = scan_rtl_files([RTL_V1])
        candidates = _by_signal(suggest_excludes(bins, rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 1}))
        self.assertEqual(candidates["status_reg[16]"].disposition, SUGGESTED_EXCLUDE)
        self.assertEqual(candidates["status_reg[31]"].disposition, SUGGESTED_EXCLUDE)

    def test_untestable_by_rtl_stays_unexplained(self):
        bins = parse_urg_text_file(TGL_V1)
        rtl = scan_rtl_files([RTL_V1])
        candidates = _by_signal(suggest_excludes(bins, rtl, {"NUM_GPIO": 8, "ENABLE_IRQ": 1}))
        self.assertEqual(candidates["gpio_out[3]"].disposition, UNEXPLAINED_GAP)

    def test_formatter_requires_approval_and_note_for_overrides(self):
        entries = [
            {"id": "i::pslverr", "instance": "i", "signal": "pslverr", "disposition": SUGGESTED_EXCLUDE,
             "reason": "tie-off", "source": "f.v:1", "approved": True, "note": None},
            {"id": "i::gpio_out[3]", "instance": "i", "signal": "gpio_out[3]", "disposition": UNEXPLAINED_GAP,
             "reason": "no justification", "source": "", "approved": True, "note": None},
            {"id": "i::unused", "instance": "i", "signal": "unused", "disposition": SUGGESTED_EXCLUDE,
             "reason": "tie-off", "source": "f.v:2", "approved": False, "note": None},
        ]
        text, warnings = format_ucm_exclusions(entries)
        self.assertIn('"pslverr"', text)
        self.assertNotIn('"unused"', text)
        self.assertNotIn('"gpio_out[3]"', text)
        self.assertEqual(len(warnings), 1)
        self.assertIn("gpio_out[3]", warnings[0])

    def test_formatter_allows_override_with_note(self):
        entries = [
            {"id": "b", "instance": "i", "signal": "gpio_out[3]", "disposition": UNEXPLAINED_GAP,
             "reason": "no justification", "source": "", "approved": True, "note": "BUG-1234, waived temporarily"},
        ]
        text, warnings = format_ucm_exclusions(entries)
        self.assertIn('"gpio_out[3]"', text)
        self.assertIn("MANUAL OVERRIDE", text)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
