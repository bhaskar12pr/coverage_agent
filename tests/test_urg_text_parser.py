import os
import unittest

from coverage_agent.parsers.urg_text import parse_urg_text_file
from coverage_agent.report import build_gap_report

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "samples", "urg_text", "tgl_report.txt")


class TestUrgTextParser(unittest.TestCase):
    def test_parses_all_bins(self):
        bins = parse_urg_text_file(SAMPLE)
        self.assertEqual(len(bins), 23)

    def test_instance_scoping(self):
        bins = parse_urg_text_file(SAMPLE)
        instances = {b.instance for b in bins}
        self.assertEqual(
            instances,
            {"tb_top.dut.u_axi_vip.master_agent", "tb_top.dut.u_axi_vip.slave_agent"},
        )

    def test_never_toggled_detected(self):
        bins = parse_urg_text_file(SAMPLE)
        never = {b.signal for b in bins if b.never_toggled}
        self.assertIn("awready", never)
        self.assertIn("wdata[6]", never)
        self.assertIn("bresp[1]", never)
        self.assertIn("rvalid", never)

    def test_missing_single_direction(self):
        bins = parse_urg_text_file(SAMPLE)
        by_signal = {b.signal: b for b in bins}
        self.assertFalse(by_signal["awvalid"].hit_1_to_0)
        self.assertTrue(by_signal["awvalid"].hit_0_to_1)
        self.assertFalse(by_signal["awaddr[1]"].hit_0_to_1)
        self.assertTrue(by_signal["awaddr[1]"].hit_1_to_0)

    def test_fully_covered(self):
        bins = parse_urg_text_file(SAMPLE)
        by_signal = {b.signal: b for b in bins}
        self.assertTrue(by_signal["aclk"].fully_covered)

    def test_gap_report_scope_filters_by_instance(self):
        bins = parse_urg_text_file(SAMPLE)
        report = build_gap_report(bins, scope="tb_top.dut.u_axi_vip.slave_agent")
        self.assertEqual(report.total, 7)
        self.assertTrue(all(b.instance.startswith("tb_top.dut.u_axi_vip.slave_agent") for b in report.never_toggled))


if __name__ == "__main__":
    unittest.main()
