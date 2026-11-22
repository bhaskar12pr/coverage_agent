"""Real-format urg modinfo.txt parsing, against unmodified excerpts of
real VCS reports published by OpenTitan (samples/urg_real/).

The strongest check: urg prints its own bit totals at the top of every
toggle section ("Total Bits 0->1  729  713"). The parsed rows must add
up to exactly those numbers, per section, per direction.
"""

import os
import unittest

from coverage_agent.model import COVERED, EXCLUDED, NOT_COVERED, UNREACHABLE, bit_width
from coverage_agent.parsers.urg_modinfo import parse_urg_modinfo
from coverage_agent.parsers.urg_text import parse_urg_text
from coverage_agent.report import build_gap_report

HERE = os.path.dirname(__file__)
REAL = os.path.join(HERE, "..", "samples", "urg_real")


def _load(*parts):
    with open(os.path.join(REAL, *parts), encoding="utf-8") as f:
        return f.read()


def _section_bits(section, direction):
    status = (lambda b: b.status_0_to_1) if direction == "0->1" else (lambda b: b.status_1_to_0)
    counted = [b for b in section.bins if status(b) not in (EXCLUDED, UNREACHABLE)]
    return sum(b.width for b in counted), sum(b.width for b in counted if status(b) == COVERED)


class TestBitWidth(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(bit_width("data_i[56:6]"), 51)
        self.assertEqual(bit_width("dio_attr_i[0].invert[0:0]"), 1)
        self.assertEqual(bit_width("alert_rx_i[0].ack_n"), 1)  # index inside the name, not a range
        self.assertEqual(bit_width("tl_o.d_user.rsp_intg[6]"), 1)
        self.assertEqual(bit_width("clk_i"), 1)


class TestTotalsMatchUrg(unittest.TestCase):
    def _assert_all_sections_match(self, text):
        result = parse_urg_modinfo(text)
        self.assertTrue(result.sections)
        for s in result.sections:
            self.assertEqual(s.unparsed_rows, [], s.name)
            for d, (tot, cov) in (
                ("0->1", (s.total_bits_0_to_1, s.covered_bits_0_to_1)),
                ("1->0", (s.total_bits_1_to_0, s.covered_bits_1_to_0)),
            ):
                self.assertEqual(_section_bits(s, d), (tot, cov), f"{s.kind} {s.name} {d}")
        return result

    def test_earlgrey_padring(self):
        r = self._assert_all_sections_match(_load("opentitan_earlgrey", "modinfo.txt"))
        (s,) = r.sections
        self.assertEqual((s.kind, s.name, s.has_exclusions), ("instance", "tb.dut.u_padring", True))
        self.assertEqual((s.total_bits_0_to_1, s.covered_bits_0_to_1), (729, 713))

    def test_gpio_module(self):
        self._assert_all_sections_match(_load("opentitan_gpio", "modinfo.txt"))

    def test_rv_dm(self):
        self._assert_all_sections_match(_load("opentitan_rv_dm", "modinfo.txt"))


class TestRowDetail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eg = {b.signal: b for b in parse_urg_modinfo(_load("opentitan_earlgrey", "modinfo.txt")).bins}
        cls.gpio = {b.signal: b for b in parse_urg_modinfo(_load("opentitan_gpio", "modinfo.txt")).bins}
        cls.rv_dm = parse_urg_modinfo(_load("opentitan_rv_dm", "modinfo.txt"))

    def test_partial_direction_range(self):
        b = self.eg["dio_in_raw_o[8:3]"]
        self.assertEqual((b.status_0_to_1, b.status_1_to_0), (COVERED, NOT_COVERED))
        self.assertEqual(b.tests_0_to_1, ("T21", "T49", "T22"))
        self.assertEqual(b.tests_1_to_0, ())
        self.assertEqual((b.width, b.direction), (6, "OUTPUT"))
        self.assertTrue(b.is_gap)

    def test_excluded_with_annotation(self):
        b = self.eg["dio_attr_i[0].invert[0:0]"]
        self.assertTrue(b.excluded)
        self.assertFalse(b.is_gap)
        self.assertEqual(b.annotation, "[UNR] Tie offs.")
        self.assertEqual(b.direction, "INPUT")

    def test_unreachable(self):
        b = self.gpio["tl_i.a_param[2:0]"]
        self.assertEqual((b.status_0_to_1, b.status_1_to_0), (UNREACHABLE, UNREACHABLE))
        self.assertFalse(b.is_gap)

    def test_star_tests_marker_kept(self):
        b = self.gpio["tl_o.d_user.rsp_intg[5:0]"]
        self.assertEqual(b.tests_1_to_0, ("*T41", "*T42", "*T43"))

    def test_instance_to_module_map(self):
        r = parse_urg_modinfo(_load("opentitan_earlgrey", "modinfo.txt"))
        self.assertEqual(r.instance_module.get("tb.dut.u_padring"), "padring")
        self.assertTrue(all(b.module == "padring" for b in r.bins))

    def test_module_section_kept_when_no_instances_known(self):
        # rv_dm excerpt has no "Module Instance" blocks: module section stays module-scoped
        mods = [b for b in self.rv_dm.bins if b.scope_kind == "module"]
        self.assertTrue(mods)
        self.assertTrue(all(b.instance == "prim_alert_sender" for b in mods))

    def test_line_section_is_ignored(self):
        # the Earlgrey excerpt carries a full Line Coverage section for the same instance
        names = set(self.eg)
        self.assertFalse(any(n.isdigit() or n in ("ALWAYS", "CONT_ASSIGN") for n in names))


class TestDispatchAndReport(unittest.TestCase):
    def test_parse_urg_text_dispatches_to_modinfo(self):
        bins = parse_urg_text(_load("opentitan_earlgrey", "modinfo.txt"))
        self.assertTrue(any(b.status_0_to_1 for b in bins))

    def test_gap_report_skips_waived_and_matches_section_totals(self):
        bins = parse_urg_text(_load("opentitan_earlgrey", "modinfo.txt"))
        rep = build_gap_report(bins)
        # urg: 729+729 bits, 713+695 covered
        self.assertEqual((rep.transitions_total, rep.transitions_covered), (1458, 1408))
        self.assertTrue(rep.waived > 0)
        self.assertFalse(any(b.excluded for b in rep.never_toggled + rep.missing_0_to_1 + rep.missing_1_to_0))


if __name__ == "__main__":
    unittest.main()
