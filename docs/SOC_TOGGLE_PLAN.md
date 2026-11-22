# SoC toggle coverage plan

Goal: take `coverage_agent` from toggle coverage of one VIP to toggle
coverage of a whole SoC (VCS / `urg` first, Xcelium / IMC later),
including **closing gaps across regressions** and **carrying exclusions
correctly into derivative projects**.

Reference data: OpenTitan's public VCS reports
(`reports.opentitan.org`) and exclusion files (`hw/top_earlgrey/dv/cov`),
Apache 2.0. `top_earlgrey` is the main OpenTitan chip (Ibex RISC-V,
TileLink-UL bus, ~30 IPs); `top_darjeeling` is a real derivative of it
(shares IPs, differs in parameters, e.g. GPIO straps). Excerpts used by
the tests live in `samples/urg_real/` — see its `ATTRIBUTION.md`.

---

## 1. Coverage improvement loop

The tool never runs the simulator. You run the regression, the tool
decides what to do next, and every new report is compared with the last.

**Inputs:** merged `urg` report (`modinfo.txt` from `-format text|both`),
RTL file list (same as the VCS compile), project/derivative config
(params, defines, top), existing `.el` / `-node` exclusions, optionally
the register map (for register-level stimulus).

**Every gap is sorted into one group:**

| Group | Meaning | Next step |
|---|---|---|
| Dead by design | Proven from RTL: tie-off, parameter-disabled, unconnected at top level | Proposed exclusion, needs approval |
| Probably dead | No proof, looks unreachable (LLM or rule guess) | Proposed exclusion, needs approval **and** a written reason |
| Missing test | Feature never exercised (e.g. IRQ never enabled) | Stimulus / test suggestion |
| Missing value | Some bits/one direction only (high address bits, wide data) | Constraint or sequence change |
| Integration | IP port at SoC level never driven by the system | Connectivity test, or flag as possible SoC bug |

**Suggestions use the report's `Tests` column:** urg lists which tests
toggled each signal (`data_i[5:0] … T1,T10,T7` while `data_i[56:6]` is
never hit), so a suggestion can say *extend T7* instead of *write a new
test*. Ordered by bits closed, grouped by IP owner.

**On the next report:** closed gaps (and which test closed them), still
open (suggestion tried and failed -> "maybe dead, look again"), new gaps
(RTL change), and stale exclusions (excluded signal now toggles, or
names a signal that no longer exists).

**Gap tracking file (JSON):** status per gap
(open / suggested / test written / closed / excluded), owner, history.

## 2. Derivative projects

Exclusions are **not copied** between projects. Each exclusion stores
*why* it is valid, and the reason is re-checked on the derivative:

```
signal : top.u_gpio.irq
reason : param ENABLE_IRQ=0 disables the generate block (gpio.sv:120)
layer  : ip/gpio
```

| Result | Meaning | Action |
|---|---|---|
| Carried over | Reason still holds with this derivative's RTL + params | Kept, listed in the report |
| Dropped | Reason no longer holds (e.g. `ENABLE_IRQ=1` here) | Removed and flagged as a **real gap** |
| New | Dead code only in this derivative | Proposed, needs approval |
| Moved | Same signal, different instance path / renamed | Re-mapped, needs approval |

Exclusions are layered so each is written once and merged per chip:

```
excludes/
  ip/gpio/          # valid wherever this IP is used with these params
  soc/common/       # SoC integration tie-offs shared by all chips
  soc/earlgrey/     # this chip only
  soc/darjeeling/   # this derivative only
```

Output: the merged `.el` per chip plus a derivative diff report
(carried / dropped / new / moved) so review is only of what changed.

The "moved" case is real in the OpenTitan data: the Feb-2025 Earlgrey
`.el` still excludes `top_earlgrey.ram_2p_cfg_i.*` and
`u_padring.dio_attr_i[23].*`, but the report has `spi_ram_2p_cfg_i` and
only DIO pads `[0]..[22]` — stale exclusions, and the renamed bits show
up as real never-toggled gaps.

## 3. Phases

### Phase 0 — real data (VCS)
- [x] Parser for real `urg` `modinfo.txt` toggle sections
      (`coverage_agent/parsers/urg_modinfo.py`): bit ranges, struct
      fields, `Yes/No/Unreachable/Excluded`, per-direction tests,
      INPUT/OUTPUT/INOUT, exclude annotations, `(x)` marker,
      instance->module map, module/instance de-duplication.
      **Validated:** parsed bits equal urg's own section totals for all
      96 toggle sections in the rv_dm, gpio and top_earlgrey reports, and
      the computed toggle score equals the dashboard (89.91%, 100%,
      95.36%).
- [x] Gap report skips Excluded/Unreachable rows and reports the
      bit-weighted toggle score; a `urgReport/` directory reads only
      `modinfo.txt`.
- [x] Reader for real `.el` files (`INSTANCE:`/`MODULE:`/`ANNOTATION:`,
      whole-signal, single-bit, single-direction, non-toggle metrics)
      and `-node` cfg exclusions. **Validated:** all 1,122 Excluded rows
      in the full Earlgrey report trace back to the 12 real `.el` files.
- [x] `verify-excludes` works on real `.el` + real report: matches `.el`
      names to urg's range rows, reports stale entries as
      `unknown_signal`, audits rows urg shows as Excluded as gaps (urg
      hides their hits) instead of calling them redundant.
- [ ] `.el` **writer** in the real format. Open question: VCS writes a
      `CHECKSUM:` per scope that this tool can't compute — confirm
      whether your VCS version accepts an `.el` without it (or with a
      stale one) before loading generated files.
- [ ] Run `suggest-excludes` on real RTL (OpenTitan GPIO, then
      Earlgrey). Needs Phase 2 for most SoC-level tie-offs.

### Phase 1 — SoC data model
- [ ] Instance tree from the report (instance -> module -> params).
- [ ] Ownership config: instance prefix -> IP / subsystem / owner; rollup.
- [ ] Ports-only mode (SoC integration view) — OpenTitan already scores
      `tgl(portsonly)`.
- [ ] Streaming parse for very large reports (Earlgrey full report:
      28 MB, ~0.9 s today — fine; revisit at 10x).

### Phase 2 — integration-level dead code
- [ ] Constant / unconnected ports at instantiation
      (`.irq_i(1'b0)`, `.dio_attr_i(...)` from tied-off pinmux outputs),
      propagated into the child IP. This is what would *prove* the 501
      padring tie-offs that `verify-excludes` can only flag today.
- [ ] Scanner: `always_comb`, loop guards, struct field names
      (`hw2reg.x.de`), case-generate.
- [ ] **Decision:** optional `pyslang` (full elaboration, per-instance
      params) with regex fallback, so locked-down environments still work.

### Phase 3 — exclusion categories and reuse
- [ ] Category per exclusion: tie-off, param-gated, DFT, strap,
      power-gated, reserved, connectivity, LOW_RISK (covered in formal —
      OpenTitan uses this tag). Different approval rules per category.
- [ ] Layered exclusions + derivative diff (section 2).

### Phase 4 — closing gaps
- [ ] Test grading from the `Tests` column; minimal test set.
- [ ] Regression-to-regression delta + gap tracking file.
- [ ] Register-map driven stimulus (OpenTitan `hjson`, or your format).

### Phase 5 — reporting and skills
- [ ] HTML dashboard per IP owner; CI gate on unapproved / suspicious
      exclusions.
- [ ] Claude Code skills in `Skill-collection-`: toggle triage, exclusion
      review, SoC integration check, stimulus generation — each calling
      `coverage_agent`.

### Later — Xcelium / IMC
- [ ] IMC text toggle report parser + your own text exclusion format
      (needs a sample of each). Everything above the parser is
      simulator-independent.
