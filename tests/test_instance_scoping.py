"""Regression test for a real bug found while scanning a 4-IP SoC:
the classifier had no concept of WHICH INSTANCE a tie-off belongs to,
so when the same signal name (PREADY/PSLVERR — extremely common
across APB peripherals) is tied off in multiple different files, it
returned the first match by name alone regardless of which instance
was actually being classified — citing the wrong file as evidence.

Fix: RtlFacts now tracks module_by_file (first `module <name>` per
file) and instance_to_module (harvested from instantiations found
while scanning, e.g. an SoC-integration file). _classify_never_toggled
resolves a candidate's leaf instance name to its module, then only
searches tie-offs/gates from that module's own file(s). Falls back to
the old "search everything" behavior when the instance isn't found in
any instantiation (the common single-IP-scan case, no SoC-integration
file in the scan set) — see test_no_instance_info_falls_back_to_old_behavior.
"""

import os
import tempfile
import unittest

from coverage_agent.exclude.candidates import SUGGESTED_EXCLUDE, UNEXPLAINED_GAP, suggest_excludes
from coverage_agent.model import ToggleBin
from coverage_agent.rtl.scanner import scan_rtl_files

# ip_a really ties PSLVERR off. ip_b does NOT — it's real, conditional
# logic (mirrors apb_timer.sv's real PREADY/PSLVERR: an always_comb
# if/else, which this scanner doesn't recognize as a tie-off — on
# purpose, since it genuinely isn't an unconditional one).
IP_A = """
module ip_a (input a, output b, output PSLVERR);
  assign PSLVERR = 1'b0;
endmodule
"""

IP_B = """
module ip_b (input a, output b, output logic PSLVERR);
  always @(*) begin
    if (a) PSLVERR = 1'b1;
    else PSLVERR = 1'b0;
  end
endmodule
"""

SOC_TOP = """
module soc_top (input clk, input a, output b1, output b2, output ps1, output ps2);
  ip_a u_ip_a (.a(a), .b(b1), .PSLVERR(ps1));
  ip_b u_ip_b (.a(a), .b(b2), .PSLVERR(ps2));
endmodule
"""


def _write_files(d: str) -> dict[str, str]:
    paths = {}
    for name, text in [("ip_a.v", IP_A), ("ip_b.v", IP_B), ("soc_top.v", SOC_TOP)]:
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(text)
        paths[name] = p
    return paths


class TestInstanceScoping(unittest.TestCase):
    def test_same_signal_name_different_instances_resolved_independently(self):
        with tempfile.TemporaryDirectory() as d:
            paths = _write_files(d)
            facts = scan_rtl_files([paths["ip_a.v"], paths["ip_b.v"], paths["soc_top.v"]])

            bins = [
                ToggleBin(instance="soc_top.u_ip_a", signal="PSLVERR", hit_0_to_1=False, hit_1_to_0=False),
                ToggleBin(instance="soc_top.u_ip_b", signal="PSLVERR", hit_0_to_1=False, hit_1_to_0=False),
            ]
            candidates = {c.instance: c for c in suggest_excludes(bins, facts, {})}

            # u_ip_a's PSLVERR IS a real tie-off, and must cite ip_a.v.
            self.assertEqual(candidates["soc_top.u_ip_a"].disposition, SUGGESTED_EXCLUDE)
            self.assertIn("ip_a.v", candidates["soc_top.u_ip_a"].source)

            # u_ip_b's PSLVERR is NOT a tie-off — must NOT be excluded,
            # and must NOT be (mis)attributed to ip_a.v just because
            # the signal name matches something found there.
            self.assertEqual(candidates["soc_top.u_ip_b"].disposition, UNEXPLAINED_GAP)

    def test_no_instance_info_falls_back_to_old_behavior(self):
        """Scanning just ip_a.v alone (the common single-IP case, no
        SoC-integration file to learn instance->module from) must
        still work — instance name is unresolvable, so fall back to
        searching every scanned file, same as before this fix."""
        with tempfile.TemporaryDirectory() as d:
            paths = _write_files(d)
            facts = scan_rtl_files([paths["ip_a.v"]])

            bins = [ToggleBin(instance="tb.dut.u_ip_a", signal="PSLVERR", hit_0_to_1=False, hit_1_to_0=False)]
            candidates = {c.instance: c for c in suggest_excludes(bins, facts, {})}
            self.assertEqual(candidates["tb.dut.u_ip_a"].disposition, SUGGESTED_EXCLUDE)


if __name__ == "__main__":
    unittest.main()
