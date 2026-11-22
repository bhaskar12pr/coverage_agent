"""pyslang constant-propagation engine (coverage_agent/rtl/slang_const.py).

The design below reproduces, in miniature, every shape the engine had to
follow on the real OpenTitan top_earlgrey padring (see
docs/SOC_TOGGLE_PLAN.md): a parameter-selected always_comb mask, a
`register & mask` inside a for-generate, a reordered concatenation into
a child port, a struct assignment pattern with default, plus things that
must stay UNKNOWN (flops, a combinational loop). Skipped without pyslang.
"""

import os
import shutil
import tempfile
import unittest

try:
    import pyslang  # noqa: F401

    HAVE_PYSLANG = True
except ImportError:
    HAVE_PYSLANG = False

DESIGN = r"""
package p_pkg;
  typedef struct packed {
    logic [3:0] ds;     // bits 7:4
    logic [1:0] slew;   // bits 3:2
    logic       keep;   // bit 1
    logic       inv;    // bit 0
  } attr_t;
  typedef enum logic [1:0] {Bidir = 2'd0, InOnly = 2'd1} ptype_e;
endpackage

module mask_gen import p_pkg::*; #(parameter ptype_e T = Bidir) (output attr_t m);
  if (T == InOnly) begin : g_in
    always_comb begin
      m = '0;
      m.inv = 1'b1;
    end
  end else begin : g_bi
    always_comb begin
      m = '0;
      m.inv = 1'b1;
      m.ds[0] = 1'b1;
    end
  end
endmodule

module mux import p_pkg::*; (
  input  logic             clk,
  input  attr_t [1:0]      reg_d,
  output attr_t [1:0]      attr_o
);
  attr_t [1:0] q;
  always_ff @(posedge clk) q <= reg_d;   // registers: never constant
  for (genvar k = 0; k < 2; k++) begin : g
    attr_t mk;
    mask_gen #(.T(k == 0 ? Bidir : InOnly)) u_m (.m(mk));
    assign attr_o[k] = q[k] & mk;
  end
endmodule

module pads import p_pkg::*; #(parameter bit HasLoop = 1'b1) (
  input  attr_t [2:0] attr_i,
  input  logic        z_i,
  output logic        o
);
  logic x, y, gated;
  assign x = y;                 // combinational loop: must stay unknown
  assign y = x | z_i;
  always_comb begin
    if (HasLoop) gated = 1'b0;  // constant-condition if
    else gated = z_i;
  end
  assign o = ^attr_i ^ x ^ gated;
endmodule

module chip import p_pkg::*; (
  input  logic        clk,
  input  attr_t [1:0] d,
  input  logic        z,
  output logic        o
);
  attr_t [1:0] a;
  attr_t manual;
  assign manual = '{keep: 1'b1, default: '0};
  mux  u_mux  (.clk(clk), .reg_d(d), .attr_o(a));
  pads u_pads (.attr_i({a[0], a[1], manual}), .z_i(z), .o(o));
endmodule
"""


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class TestConstAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from coverage_agent.rtl.slang_const import ConstAnalysis

        cls.tmp = tempfile.mkdtemp()
        path = os.path.join(cls.tmp, "chip.sv")
        with open(path, "w") as f:
            f.write(DESIGN)
        cls.a = ConstAnalysis(f"{path} --top chip", {"tb.dut": "chip"})
        rows = [("tb.dut.u_pads", s) for s in (
            "attr_i[0].keep[0:0]", "attr_i[0].inv[0:0]", "attr_i[1].ds[3:0]", "attr_i[1].inv[0:0]",
            "attr_i[2].ds[3:1]", "attr_i[2].ds[0]", "attr_i[2].slew[1:0]",
        )]
        cls.a.prepare_rows(rows)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def const(self, signal, inst="tb.dut.u_pads"):
        p = self.a.row_constant(inst, signal)
        return None if p is None else p.value_str

    def test_compiles_cleanly(self):
        self.assertEqual(self.a.error_count, 0)
        self.assertIn("chip.u_pads", self.a.instances)

    def test_assignment_pattern_with_default(self):
        # attr_i[0] = manual = '{keep: 1, default: '0}
        self.assertEqual(self.const("attr_i[0].keep[0:0]"), "1'b1")
        self.assertEqual(self.const("attr_i[0].inv[0:0]"), "1'b0")

    def test_mask_through_generate_and_reordered_concat(self):
        # attr_i[1] = a[1] = q[1] & mask(InOnly: only inv)
        self.assertEqual(self.const("attr_i[1].ds[3:0]"), "4'b0000")
        self.assertIsNone(self.const("attr_i[1].inv[0:0]"))  # q & 1 -> follows the register
        # attr_i[2] = a[0] = q[0] & mask(Bidir: inv, ds[0])
        self.assertEqual(self.const("attr_i[2].ds[3:1]"), "3'b000")
        self.assertIsNone(self.const("attr_i[2].ds[0]"))
        self.assertEqual(self.const("attr_i[2].slew[1:0]"), "2'b00")

    def test_flops_loops_and_inputs_stay_unknown(self):
        mux = self.a.instances["chip.u_mux"]
        q = self.a._lookup(mux, "q")
        self.assertTrue(all(b is None for b in self.a.value_of(q)))
        pads = self.a.instances["chip.u_pads"]
        for name in ("x", "y", "z_i"):
            sym = self.a._lookup(pads, name)
            self.assertEqual(self.a.value_of(sym), [None], name)

    def test_constant_if_condition(self):
        pads = self.a.instances["chip.u_pads"]
        self.assertEqual(self.a.value_of(self.a._lookup(pads, "gated")), [0])

    def test_chain_follows_the_right_bit(self):
        chain = self.a.row_constant("tb.dut.u_pads", "attr_i[2].ds[3:1]").chain
        text = "\n".join(chain)
        self.assertIn("chip.a", text)
        self.assertIn("g[0].mk", text)          # pad 2 is a[0] -> mask of generate index 0
        self.assertNotIn("g[1].mk", text)
        self.assertIn("always_comb", chain[-1])
        chain0 = self.a.row_constant("tb.dut.u_pads", "attr_i[0].keep[0:0]").chain
        self.assertIn("chip.manual", "\n".join(chain0))

    def test_unresolvable_row(self):
        self.assertIsNone(self.a.resolve_row("tb.dut.u_pads", "no_such_port"))
        self.assertIsNone(self.a.resolve_row("tb.dut.u_missing", "attr_i[0].keep"))


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class TestSuggestExcludesWithSlang(unittest.TestCase):
    """End to end: a real-format report row the regex scanner can't
    explain becomes a suggested exclude backed by the pyslang proof;
    a register-driven bit stays an unexplained gap."""

    REPORT = """Toggle Coverage for Instance : tb.dut.u_pads
Port Details
                     Toggle Toggle 1->0 Toggle 0->1 Direction
attr_i[2].ds[3:1]    No     No          No          INPUT
attr_i[2].inv[0:0]   No     No          No          INPUT
-------------------------------------------------------------------------------
"""

    def test_cli_suggest_excludes(self):
        import json
        import subprocess
        import sys

        tmp = tempfile.mkdtemp()
        try:
            sv = os.path.join(tmp, "chip.sv")
            rpt = os.path.join(tmp, "modinfo.txt")
            cfg = os.path.join(tmp, "cfg.json")
            out = os.path.join(tmp, "cand.json")
            with open(sv, "w") as f:
                f.write(DESIGN)
            with open(rpt, "w") as f:
                f.write(self.REPORT)
            with open(cfg, "w") as f:
                json.dump({"project": "t", "params": {}}, f)
            root = os.path.join(os.path.dirname(__file__), "..")
            r = subprocess.run(
                [sys.executable, "-m", "coverage_agent", "suggest-excludes", rpt, "--config", cfg,
                 "--slang", f"{sv} --top chip", "--hier-map", "tb.dut=chip", "--out", out],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(r.returncode in (0, 1), True, r.stderr)
            with open(out) as f:
                cands = {c["signal"]: c for c in json.load(f)}
            self.assertEqual(cands["attr_i[2].ds[3:1]"]["disposition"], "suggested_exclude")
            self.assertIn("pyslang", cands["attr_i[2].ds[3:1]"]["reason"])
            self.assertIsNone(cands["attr_i[2].ds[3:1]"]["approved"])  # never auto-approved
            self.assertEqual(cands["attr_i[2].inv[0:0]"]["disposition"], "unexplained_gap")
            self.assertIn("not constant in the elaborated design", cands["attr_i[2].inv[0:0]"]["reason"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class TestContradictions(unittest.TestCase):
    """A row that toggled although the RTL holds it constant (testbench
    force) is reported; a register-driven row that toggled is not."""

    def test_forced_constant_row_is_reported(self):
        from coverage_agent.model import COVERED, ToggleBin
        from coverage_agent.rtl.slang_const import ConstAnalysis

        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "chip.sv")
            with open(path, "w") as f:
                f.write(DESIGN)
            a = ConstAnalysis(f"{path} --top chip", {"tb.dut": "chip"})
            forced = ToggleBin("tb.dut.u_pads", "attr_i[2].ds[3:1]", True, True, COVERED, COVERED)
            live = ToggleBin("tb.dut.u_pads", "attr_i[2].inv[0:0]", True, True, COVERED, COVERED)
            found = a.contradictions([forced, live])
            self.assertEqual([b.signal for b, _ in found], ["attr_i[2].ds[3:1]"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
