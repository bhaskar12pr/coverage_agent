"""Real VCS .el / -node exclusion parsing, against unmodified OpenTitan
top_earlgrey exclusion files (samples/urg_real/opentitan_earlgrey/), and
cross-checked against the urg report those files were applied to."""

import os
import unittest

from coverage_agent.exclude.el_parser import (
    entry_covers_row,
    parse_cov_cfg_nodes,
    parse_ucm_exclusion_file,
    parse_vcs_el,
)
from coverage_agent.parsers.urg_modinfo import parse_urg_modinfo

EG = os.path.join(os.path.dirname(__file__), "..", "samples", "urg_real", "opentitan_earlgrey")


def _load(name):
    with open(os.path.join(EG, name), encoding="utf-8") as f:
        return f.read()


class TestVcsEl(unittest.TestCase):
    def test_instance_scoped_toggle_with_annotation(self):
        entries = parse_vcs_el(_load("spi_host_device_unr_tied_off.el"))
        toggles = [e for e in entries if e.metric == "toggle"]
        self.assertEqual(len(toggles), 11)
        e = next(e for e in toggles if e.scope.endswith("u_spi_host1") and e.signal == "passthrough_i.csb")
        self.assertEqual(e.scope_kind, "instance")
        self.assertEqual(e.annotation, "[UNR] Tied off.")
        self.assertEqual(e.decl, "logic passthrough_i.csb")
        self.assertEqual(e.bit, "")

    def test_single_bit_entries(self):
        entries = [e for e in parse_vcs_el(_load("rom_ctrl_kmac_app_tied_off_data_strb_bits.el")) if e.metric == "toggle"]
        bit = next(e for e in entries if e.bit == "39")
        self.assertEqual(bit.signal, "app_i[2].data")
        self.assertEqual(bit.signal_with_bit, "app_i[2].data[39]")
        self.assertEqual(bit.decl, "logic app_i[2].data[63:0]")

    def test_module_scope_and_non_toggle_metrics(self):
        entries = parse_vcs_el(_load("plic_le_i_tied_off.el"))
        self.assertEqual(sorted(e.metric for e in entries), ["branch", "condition"])
        self.assertTrue(all(e.annotation.startswith("[UNR]") for e in entries))

    def test_annotation_applies_to_next_entry_only(self):
        for e in parse_vcs_el(_load("conn_ast_mem_cfg.el")):
            self.assertTrue(e.annotation, f"line {e.line} lost its annotation")

    def test_legacy_pairs_api_reads_real_files(self):
        pairs = parse_ucm_exclusion_file(_load("spi_host_device_unr_tied_off.el"))
        self.assertIn(("tb.dut.top_earlgrey.u_spi_host1", "passthrough_i.csb"), pairs)


class TestNodeCfg(unittest.TestCase):
    def test_wildcard_nodes(self):
        nodes = parse_cov_cfg_nodes(_load("clkmgr_tgl_excl.cfg"))
        self.assertTrue(nodes)
        n = nodes[0]
        self.assertTrue(n.matches("tb.dut.top_earlgrey.u_clkmgr_aon.cg_en_o.aon_powerup"))
        self.assertFalse(n.matches("tb.dut.top_earlgrey.u_clkmgr_aon.cg_en_o.aon_peri"))

    def test_two_part_node(self):
        nodes = parse_cov_cfg_nodes(_load("chip_cover_tgl.cfg"))
        n = next(n for n in nodes if "u_spi_host1" in n.pattern and "passthrough_i" in n.pattern)
        self.assertTrue(n.matches("tb.dut.top_earlgrey.u_spi_host1.passthrough_i.csb_en"))


class TestElAgainstRealReport(unittest.TestCase):
    """Every Excluded row urg printed for tb.dut.u_padring must be
    explained by an entry of the .el that produced it. In the other
    direction, the only entries that hit no row are dio_attr_i[23].* —
    a real stale exclusion: this padring only has DIO pads [0]..[22]."""

    def test_report_and_el_agree(self):
        bins = parse_urg_modinfo(_load("modinfo.txt")).bins
        entries = [e for e in parse_vcs_el(_load("pinmux_pad_attr_padring.el")) if e.metric == "toggle"]
        excluded = [b for b in bins if b.excluded]
        self.assertGreater(len(excluded), 100)
        for b in excluded:
            self.assertTrue(any(entry_covers_row(e, b.instance, b.signal, b.module) for e in entries), b.full_name)
        stale = sorted(
            e.signal for e in entries if not any(entry_covers_row(e, b.instance, b.signal, b.module) for b in bins)
        )
        self.assertEqual(len(stale), 9)
        self.assertTrue(all(s.startswith("dio_attr_i[23].") for s in stale), stale)
        # and nothing the .el names is reported as a gap
        for b in bins:
            if any(entry_covers_row(e, b.instance, b.signal, b.module) for e in entries):
                self.assertFalse(b.is_gap, b.full_name)

    def test_bit_entry_matches_range_row(self):
        from coverage_agent.exclude.el_parser import ElEntry

        e = ElEntry(metric="toggle", scope_kind="instance", scope="i", signal="app_i[2].data", bit="39")
        self.assertTrue(entry_covers_row(e, "i", "app_i[2].data[63:32]"))
        self.assertFalse(entry_covers_row(e, "i", "app_i[2].data[31:0]"))
        self.assertFalse(entry_covers_row(e, "other", "app_i[2].data[63:32]"))


if __name__ == "__main__":
    unittest.main()


class TestVerifyOnRealData(unittest.TestCase):
    """verify-excludes against the real padring .el + report. With no
    matching RTL scanned, nothing can be *confirmed* — but the stale
    entries must come back unknown_signal, and rows urg shows as
    Excluded must never be called redundant (urg hides their hits)."""

    def test_stale_and_no_false_redundant(self):
        from coverage_agent.exclude.verify import REDUNDANT, UNKNOWN_SIGNAL, verify_exclusions
        from coverage_agent.rtl.scanner import scan_rtl_files

        bins = parse_urg_modinfo(_load("modinfo.txt")).bins
        pairs = parse_ucm_exclusion_file(_load("pinmux_pad_attr_padring.el"))
        results = verify_exclusions(pairs, bins, scan_rtl_files([]), {})
        self.assertEqual(len(results), 510)
        unknown = [r.signal for r in results if r.verdict == UNKNOWN_SIGNAL]
        self.assertEqual(len(unknown), 9)
        self.assertTrue(all(s.startswith("dio_attr_i[23].") for s in unknown))
        self.assertEqual([r for r in results if r.verdict == REDUNDANT], [])
