import os
import unittest

from coverage_agent.parsers.urg_html import parse_urg_html_file
from coverage_agent.report import build_gap_report

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "samples", "urg_html", "tgl_report.html")


class TestUrgHtmlParser(unittest.TestCase):
    def test_parses_all_bins(self):
        bins = parse_urg_html_file(SAMPLE)
        self.assertEqual(len(bins), 10)

    def test_instance_from_heading(self):
        bins = parse_urg_html_file(SAMPLE)
        self.assertTrue(all(b.instance == "tb_top.dut.u_apb_vip.apb_agent" for b in bins))

    def test_never_toggled_detected(self):
        bins = parse_urg_html_file(SAMPLE)
        never = {b.signal for b in bins if b.never_toggled}
        self.assertEqual(never, {"penable", "pslverr"})

    def test_missing_single_direction(self):
        bins = parse_urg_html_file(SAMPLE)
        by_signal = {b.signal: b for b in bins}
        self.assertFalse(by_signal["psel"].hit_1_to_0)
        self.assertFalse(by_signal["paddr[6]"].hit_0_to_1)

    def test_gap_report_coverage_pct(self):
        bins = parse_urg_html_file(SAMPLE)
        report = build_gap_report(bins)
        self.assertEqual(report.total, 10)
        self.assertEqual(report.fully_covered, 6)
        self.assertAlmostEqual(report.coverage_pct, 60.0)


if __name__ == "__main__":
    unittest.main()
