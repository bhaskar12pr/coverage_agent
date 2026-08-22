import os
import unittest

from coverage_agent.rtl.scanner import scan_rtl_files

RTL_V1 = os.path.join(os.path.dirname(__file__), "..", "soc_sample", "rtl", "apb_gpio.v")
RTL_V2 = os.path.join(os.path.dirname(__file__), "..", "soc_sample", "rtl", "apb_gpio_v2.v")


class TestRtlScanner(unittest.TestCase):
    def test_finds_unconditional_tie_offs(self):
        facts = scan_rtl_files([RTL_V1])
        names = {t.signal for t in facts.tie_offs}
        self.assertIn("pslverr", names)
        self.assertIn("pready", names)
        self.assertIn("status_reg", names)
        # irq is only tied in one generate branch, must NOT show as an
        # unconditional tie-off (that was the bug: it used to)
        self.assertNotIn("irq", names)

    def test_status_reg_range_resolves_with_config_params(self):
        facts = scan_rtl_files([RTL_V1])
        status_tieoff = next(t for t in facts.tie_offs if t.signal == "status_reg")
        self.assertTrue(status_tieoff.contains_bit(16, {"NUM_GPIO": 8}))
        self.assertTrue(status_tieoff.contains_bit(31, {"NUM_GPIO": 8}))
        self.assertFalse(status_tieoff.contains_bit(7, {"NUM_GPIO": 8}))
        # different derivative, different NUM_GPIO -> different reserved range
        self.assertFalse(status_tieoff.contains_bit(20, {"NUM_GPIO": 24}))
        self.assertTrue(status_tieoff.contains_bit(30, {"NUM_GPIO": 24}))

    def test_irq_generate_gate_branch_classification(self):
        facts = scan_rtl_files([RTL_V1])
        irq_gates = [g for g in facts.generate_gates if g.signal == "irq"]
        self.assertEqual(len(irq_gates), 1)
        gate = irq_gates[0]
        self.assertEqual(gate.condition, "ENABLE_IRQ")
        self.assertFalse(gate.true_is_tied)   # driven by irq_pending, not constant
        self.assertTrue(gate.false_is_tied)   # assign irq = 1'b0

    def test_v2_rtl_has_no_pslverr_tie_off(self):
        facts = scan_rtl_files([RTL_V2])
        names = {t.signal for t in facts.tie_offs}
        self.assertNotIn("pslverr", names)
        self.assertIn("status_reg", names)


if __name__ == "__main__":
    unittest.main()
