# coverage_agent

Toggle-coverage tooling for a single VIP, from Synopsys VCS `urg`
report output:

1. **`analyze`** — parse exported toggle reports and report which
   signals/bits never toggled or are missing one transition direction,
   scoped to your VIP's instance hierarchy.
2. **`suggest-excludes` / `gen-excludes`** — cross-reference those gaps
   against RTL (tie-offs, parameter-gated `generate` blocks) to
   suggest which gaps are legitimately dead-by-design vs. real
   verification holes, **derivative-aware** (same RTL + same gap can
   have a different verdict depending on which project's parameters
   are active). Nothing is ever auto-excluded — see below.
3. **`suggest-excludes --llm`** (optional) — for gaps the RTL scanner
   can't classify at all, ask Claude for a second opinion, held to a
   *stricter* approval bar than the scanner's own findings. Opt-in,
   adds no dependency unless used.
4. **`suggest-stimulus`** — for whatever's left after excludes are
   filtered out (the real gaps), ask Claude for concrete stimulus
   (register writes, port drives, sequencing) to actually close them.
   Advisory only — no approval gate, since a bad suggestion just wastes
   time rather than hiding a bug.
5. **`verify-excludes`** — audit an exclusion review JSON *or a real
   `.el` file* (including ones an owner hand-wrote outside this tool
   entirely) against RTL, and flag any exclusion that isn't actually
   justified — the "owners might add incorrectly" check. Pure RTL-scan
   reuse, no LLM, no new dependency.

Stdlib-only (no pip installs needed) so it runs in locked-down DV
environments.

## Status: format not yet validated against a real VCS report

This was built without access to a VCS installation, from documented
`urg` conventions (columns `Name` / `Toggle 0->1` / `Toggle 1->0`,
status `Covered`/`Not Covered` or `Yes`/`No`, generated via
`urg -dir simv.vdb -format text|html -metric tgl`). The parsers in
`coverage_agent/parsers/` match on these keywords/patterns rather than
fixed column positions, so minor formatting differences should still
parse — but **run it against a real report from your VCS version
before trusting the output**. If it parses 0 bins (you'll get a
warning on stderr) or misses signals, see Troubleshooting below.

## Generate a urg toggle report (VCS)

```sh
urg -dir simv.vdb -format text -metric tgl -hier <hier_file>   # or -format html
```

`-hier` scopes the report to your VIP's instance if you don't want the
whole testbench. Point `coverage_agent` at the resulting
`urgReport/` directory (or a single report file).

## Usage

```sh
python3 -m coverage_agent analyze <path> [--scope <hier-prefix>] [--top N] [--json out.json] [--csv out.csv]
```

- `<path>`: a single `.txt`/`.html` report file, or a directory (searched recursively for both).
- `--scope`: restrict to instances whose hierarchy starts with this prefix, e.g. `tb_top.dut.u_axi_vip`.
- `--top`: max signals listed per gap category in the console output (default 50; JSON/CSV always contain everything).
- Exit code: `0` if no gaps (or no bins found), `1` if gaps exist, `2` on a bad path/no report files found.

Example, against the synthetic samples in this repo:

```sh
python3 -m coverage_agent analyze samples --scope tb_top.dut.u_axi_vip.master_agent
```

## Output categories

- **NEVER TOGGLED** — signal saw neither `0->1` nor `1->0`. Usually the highest-value gap to chase (dead net).
- **MISSING 0->1** — toggled `1->0` at least once but never `0->1`.
- **MISSING 1->0** — toggled `0->1` at least once but never `1->0`.

## Exclude generation

Toggle coverage gaps are either real verification holes (need a test)
or legitimately dead by design (tie-off, disabled by a parameter in
this derivative). `coverage_agent` distinguishes the two by scanning
your RTL — it does **not** guess from the coverage report alone.

**No auto-waiving, ever.** `suggest-excludes` only proposes; nothing
is written to a real exclusion file until a human sets
`"approved": true` in the review JSON, and `gen-excludes` refuses to
emit a signal the RTL scanner couldn't justify unless you also add a
`"note"` explaining the manual override. A wrong exclusion hides a
real bug, so the tool's failure mode is always "ask a human" (an
`unexplained_gap`), never "assume it's fine."

```sh
# 1. Get a disposition for every gap, given one derivative's config
python3 -m coverage_agent suggest-excludes <toggle-report> \
    --config <config.json> [--rtl <file-or-dir>] [--scope <hier-prefix>] \
    --out exclude_candidates.json

# 2. Open exclude_candidates.json, set "approved": true on entries you
#    agree with (add "reviewer"/"note" as you like). Leave the rest.

# 3. Render the approved entries into an exclusion file
python3 -m coverage_agent gen-excludes exclude_candidates.json --out exclude.el
```

Example against the sample IP in this repo (`soc_sample/rtl/apb_gpio.v`):

```sh
python3 -m coverage_agent suggest-excludes samples/urg_text/apb_gpio_tgl.txt \
    --config soc_sample/configs/base_project.json --out /tmp/candidates.json
```

### What the RTL scanner recognizes

`coverage_agent/rtl/scanner.py` is a **heuristic regex scanner, not a
Verilog parser**. It recognizes exactly two dead-code shapes:

1. **Unconditional tie-offs**: `assign <name>[<hi>:<lo>] = <const>;`
   (bit ranges may use parameters, e.g. `status_reg[31:NUM_GPIO]` —
   resolved against the active config's param values, not the RTL's
   defaults, so the reserved range correctly shifts per derivative).
2. **Parameter-gated `generate if/else` blocks**: for a signal driven
   in both branches (the normal shape — e.g. `assign irq = ...` in
   each), it classifies each branch's driver as constant or not, then
   picks the branch your config's params actually select. If the
   *active* branch ties the signal to a constant, that's a suggested
   exclude; if the active branch drives it with real logic, the gap is
   real regardless of what the *inactive* branch does.

Anything else (case-generate, tie-offs inside `always` blocks,
SystemVerilog `always_comb`, nested generates) isn't recognized — a
miss falls through to `unexplained_gap`, which is the safe failure
mode (it just means "ask a human," never "silently exclude").

### Instance-aware resolution (multi-IP scans)

Scanning more than one IP together (e.g. a whole SoC) surfaced a real
bug: signal names like `PREADY`/`PSLVERR` are near-universal across
APB peripherals, so without knowing *which instance* a candidate is
for, the classifier would match the first same-named tie-off found in
*any* scanned file — wrong evidence, sometimes attached to a correct
verdict by coincidence, sometimes not.

`RtlFacts` now harvests instantiations (`module_name instance_name
(.port(...), ...)`, matched heuristically — see `_INSTANTIATION_RE` in
`scanner.py`) from every scanned file, so it knows which module each
toggle-report instance actually is. A candidate is only matched
against tie-offs/gates from its *own* module's file(s). If the
instance can't be resolved this way (the common single-IP scan, no
SoC-integration file in the scan set), it falls back to searching
every scanned file — the pre-fix behavior, so single-IP usage is
unaffected. See `tests/test_instance_scoping.py`.

### Derivative / IP-version handling

A derivative config (`soc_sample/configs/*.json`) is just
`{"project", "ip_version", "rtl_file", "params": {...}}`. Two things
this was explicitly built to get right, because getting them wrong
means silently carrying over a stale waiver:

- **Same RTL, different parameters** — `soc_sample/configs/base_project.json`
  (`ENABLE_IRQ=1`) vs. `deriv_lowpower.json` (`ENABLE_IRQ=0`): the
  *identical* `irq` toggle gap is a real bug in one and a legitimate
  exclude in the other. Verified in `tests/test_exclude_candidates.py::test_derivative_awareness_irq`.
- **Different IP version entirely** — `deriv_v2ip.json` points at
  `soc_sample/rtl/apb_gpio_v2.v`, where `pslverr` is no longer tied
  off (v2 added a real illegal-address trap). Re-scanning RTL per
  config means a v1-only tie-off is never suggested for v2. Verified
  in `test_ip_version_awareness_pslverr`.

Always pass the config that actually matches the RTL/params the
coverage was collected against — the tool has no way to detect a
mismatched config/RTL pair on its own.

### Optional: LLM-assisted judgment for the leftover gaps (`--llm`)

The RTL scanner is deterministic pattern matching — it will miss real
dead-code shapes it doesn't recognize (case-generate, tie-offs inside
`always` blocks, nested generates, etc.), and those fall through to
`unexplained_gap`. `--llm` asks Claude to look at the RTL for exactly
those leftover signals and give an opinion:

```sh
python3 -m coverage_agent suggest-excludes samples/urg_text/apb_gpio_tgl.txt \
    --config soc_sample/configs/base_project.json --out candidates.json --llm
```

Requires `pip install anthropic pydantic` and API credentials
(`ANTHROPIC_API_KEY` or `ant auth login`) — **not installed by
default**; the rest of `coverage_agent` stays dependency-free, and
`suggest-excludes` without `--llm` never imports `anthropic`/`pydantic`
at all. A failed/unauthenticated call prints a warning and falls back
to the RTL-only results rather than crashing the run.

**This is strictly lower-trust than the RTL scanner, by design:**

- Only ever runs on candidates the scanner found *zero* evidence for
  (`llm_eligible=True` — never a partial-toggle gap, which can't
  structurally be a tie-off, and never a gap the scanner already
  proved real via a live generate branch).
- Gets its own disposition, `llm_suggested_exclude`, distinct from the
  scanner's `suggested_exclude` — so a human reviewing the JSON file
  can immediately see "this came from a probabilistic read of the RTL,
  not a pattern match."
- `gen-excludes` holds an `llm_suggested_exclude` to the **same bar as
  a manual override**: `"approved": true` alone isn't enough, you must
  also write a `"note"` — same rule as overriding a plain
  `unexplained_gap`. See `format_ucm_exclusions()` in
  `coverage_agent/exclude/formatter.py`.
- The model is explicitly instructed to prefer `uncertain`/`real_gap`
  over a wrong `dead_by_design` guess, and low-confidence
  `dead_by_design` verdicts are treated the same as `real_gap` (stay
  `unexplained_gap`, just with the LLM's reasoning attached for
  context).

Tested with mocked API responses in `tests/test_llm_judge.py` (no real
credentials needed to run the suite — those tests `skip` if
`anthropic`/`pydantic` aren't installed). **Not tested against a live
API call** in this session (no credentials available in this
environment) — the prompt/schema/wiring are verified, but you should
sanity-check the actual model output on a real gap before trusting it.

## Stimulus suggestions for real gaps (`suggest-stimulus`)

Once `suggest-excludes` has filtered out everything RTL-justified as
dead, what's left (`unexplained_gap`) needs an actual test. `suggest-stimulus`
asks Claude to read the RTL and propose concrete stimulus for exactly
those signals — register writes/addresses if it can identify them from
a decode in the RTL, port drives, reset/clock sequencing — grounded in
the RTL it was given, not generic advice.

```sh
python3 -m coverage_agent suggest-stimulus <toggle-report> \
    --config <config.json> [--rtl <file-or-dir>] [--scope <hier-prefix>] \
    [--json out.json]
```

Always requires `pip install anthropic pydantic` + API credentials —
unlike `suggest-excludes`, there's no non-LLM path here (a regex
scanner can't derive test stimulus). It internally reuses the exact
same RTL-scan/classification as `suggest-excludes`, so it only ever
proposes stimulus for gaps that survived that filtering — never for a
signal the scanner already found to be a tie-off or parameter-gated
dead branch.

**Lower stakes than `suggest-excludes --llm`, on purpose — no approval
gate:** a wrong exclusion can hide a real bug forever; a wrong stimulus
suggestion just costs an engineer a few minutes trying it. Each
suggestion carries `feasible: bool` and a `confidence` level, and the
model is explicitly told to say "I don't think RTL alone gives you a
way to hit this" (`feasible=false`) rather than invent a
plausible-sounding but ungrounded sequence — that's itself useful
signal (this gap may actually be dead in a way the scanner's patterns
didn't catch, worth a second look with `--llm` on `suggest-excludes`).

Example against the sample IP (mocked end-to-end in this session — see
below):

```sh
python3 -m coverage_agent suggest-stimulus samples/urg_text/apb_gpio_tgl.txt \
    --config soc_sample/configs/base_project.json --json stimulus.json
```

Against `base_project` this finds exactly the two real gaps
(`gpio_out[3]`, `irq`) — `pslverr`/`status_reg[16]`/`status_reg[31]`
are correctly excluded from consideration since the scanner already
justified them as dead. Verified with mocked API responses in
`tests/test_llm_stimulus.py` and one full CLI-level dry run with a
mocked `anthropic.Anthropic` (this session, not a live call — same
"unverified against a real API call" caveat as `--llm` above).

## Verify existing exclusions (`verify-excludes`)

An exclusion is only as good as whoever added it — a reviewer approves
something they didn't fully check, someone hand-writes an exclusion
file outside this tool entirely, or a config's parameters change and a
once-valid exclusion quietly stops being justified. `verify-excludes`
independently re-derives what SHOULD be excluded (the exact same RTL
scan `suggest-excludes` uses) and cross-checks every exclusion under
review against it. Pure RTL-scanner reuse — no LLM, no new dependency.

```sh
python3 -m coverage_agent verify-excludes <exclude-file> \
    --report <toggle-report> --config <config.json> [--rtl <file-or-dir>] [--scope <hier-prefix>] [--all]
```

`<exclude-file>` can be either a review JSON from `suggest-excludes`
(only `"approved": true` entries are checked by default — pass `--all`
to also check unapproved ones) **or a real generated/hand-edited `.el`
file** — everything in a `.el` file is, by definition, already
excluded, so all of it is checked. Auto-detected (JSON parse first,
falls back to the `.el` line format).

Five verdicts, most to least concerning:

- **`suspicious`** — RTL finds NO justification for this exclusion; it
  looks like a real, untested gap that got excluded anyway. This is
  the "owner added it incorrectly" case. Exit code `1` if any are found.
- **`unknown_signal`** — the instance/signal isn't in the given toggle
  report at all — likely a typo, wrong instance path, or wrong scope.
  Also triggers exit code `1`.
- **`llm_only`** — the exclusion is backed by an LLM opinion, not a
  deterministic RTL match (only relevant if the review JSON went
  through `--llm`) — worth a second look, not necessarily wrong.
- **`redundant`** — the signal is already fully covered; the exclusion
  has no effect (probably stale — left over from before a test started
  hitting it).
- **`confirmed`** — RTL independently agrees this is dead-by-design. No action needed.

Demonstrated against a deliberately bad hand-written `.el` file for
the sample IP (one valid tie-off, one wrongly-excluded live signal
under `ENABLE_IRQ=1`, one wrongly-excluded untested signal, one typo'd
signal name, one redundant exclusion on an already-covered signal) —
all five came back correctly classified. Also verified the same signal
(`irq`) flips from `suspicious` under `base_project` to `confirmed`
under `deriv_lowpower`, mirroring the derivative-awareness guarantee
from `suggest-excludes`. See `tests/test_verify_excludes.py`.

### Exclusion file format: unverified, same caveat as the toggle parser

`gen-excludes` emits a documented-but-unverified VCS/UCM-style
exclusion syntax (`INSTANCE:` + `Exclude Toggle "sig" -detail "01"
-comment "..."`). You said you'd provide a real exclusion file to
match against — once you do, update `coverage_agent/exclude/formatter.py`
accordingly. Don't load the generated `.el` into a real coverage merge
until you've confirmed the syntax.

## Sample IP used for exclude-gen development (`soc_sample/`)

`soc_sample/rtl/apb_gpio.v` is a synthetic APB GPIO peripheral, built
specifically to exercise the RTL scanner — **not a real customer
design**. It has, on purpose:

- a hard tie-off (`pslverr`, `pready`)
- a reserved/tied bit range sized by a parameter (`status_reg`, width via `NUM_GPIO`)
- a parameter-gated block (interrupt logic, gone entirely when `ENABLE_IRQ=0`)
- one genuinely live, untested bit (`gpio_out[3]`) with no RTL excuse — proves the tool won't fabricate a justification

`soc_sample/rtl/apb_gpio_v2.v` is the same IP, v2: adds a real
`pslverr` (illegal-address trap) instead of tying it off, used to
prove exclusions don't get reused blindly across IP versions.

### Real external RTL: `soc_sample/external/pulp_apb_gpio/`

`apb_gpio.sv` here is **real, unmodified RTL** from
[pulp-platform/apb_gpio](https://github.com/pulp-platform/apb_gpio)
(ETH Zurich/University of Bologna, Solderpad HL v0.51 — permissive,
see `LICENSE`/`ATTRIBUTION.md` in that directory), pulled in to
validate the scanner against something not built to fit its patterns.
Un-fabricated results, `tests/test_real_pulp_gpio.py`:

- Scanner correctly finds `PSLVERR` and `PREADY` as real tie-offs
  (`assign PSLVERR = 1'b0;` / `assign PREADY = 1'b1;`).
- **Real scanner gap surfaced by real RTL**: this IP's `PAD_NUM`
  parameter (32 vs up to 64) gates its upper register bank via runtime
  `if (i < PAD_NUM)` loop guards inside `always`/`for` loops — not
  `generate if/else`. The scanner doesn't recognize that shape yet, so
  those signals correctly fall through to `unexplained_gap` (the safe
  default) rather than being misclassified either way.
- Real live signals with no matching RTL pattern (`interrupt`,
  `gpio_out[5]`) correctly stay `unexplained_gap` — proof the tool
  doesn't treat "real RTL" as license to guess.

OpenTitan's GPIO IP (`hw/top_{earlgrey,darjeeling,englishbreakfast}/ip_autogen/gpio/`,
Apache 2.0) was considered first — it's an even better real
*derivative* example (`GpioGpioAsHwStrapsEn` defaults to `0` in
earlgrey vs `1` in darjeeling, tying off vs. actually driving a whole
block of strap-sampling signals, generated per chip) — but its
register interface uses struct-field signal names
(`hw2reg.hw_straps_data_in_valid.de`), which the scanner's
`\w+`-only identifier matching doesn't parse. Worth revisiting once
the scanner supports dotted signal names; not pulled into the repo yet.

Swap in your real RTL/configs the same way once available.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

Tests run against synthetic sample reports in `samples/` (an AXI-style
and an APB-style VIP toggle report), **not** real `urg` output — they
verify the parsing/gap logic, not that the format matches your VCS
version.

## Troubleshooting

**"parsed N file(s) but found 0 toggle bins"** — the parser's
keyword/regex matching didn't recognize your report's rows. Open the
report and compare against `samples/urg_text/tgl_report.txt` or
`samples/urg_html/tgl_report.html`. The fix is almost always a small
regex tweak in `coverage_agent/parsers/urg_text.py` (see `_ROW_STATUS_RE`,
`_ROW_COUNT_RE`, `_INSTANCE_RE`) or `urg_html.py` (`_col_kind`,
`_INSTANCE_RE`) to match your report's actual header/status wording.

**Signals missing / wrong instance grouping** — the instance-header
regex (`_INSTANCE_RE` in both parsers) expects a line containing
`instance` followed by `:` and the hierarchical path. If your report
uses a different phrase (e.g. a `<caption>` or `<h3>` with different
wording), update that regex.

## Not yet built (out of scope for this pass)

- Direct UCDB (binary coverage database) parsing — text/HTML export only.
- Non-toggle coverage types (line, cond, fsm, branch, functional/covergroup).
- Verified real exclusion-file syntax — pending a real sample from you (see above).
- Verified real `urg` toggle-report syntax — pending a real sample from you (same, since day one).
- RTL scanner coverage beyond tie-offs and if/else `generate` (case-generate, `always`-block tie-offs, nested generates).
- Struct-field/dotted signal names in tie-off and generate-gate detection (`reg2hw.foo.d` style) — confirmed real gap via OpenTitan's GPIO IP, see above.
- Runtime loop-bound-gated dead code (`if (i < PARAM)` inside `always`/`for`, not `generate if/else`) — confirmed real gap via pulp-platform's `apb_gpio.sv`, see above.
- `--llm` / `suggest-stimulus` verified against a live API call — built and tested with mocks only, no credentials available in this environment.
- Actually feeding `suggest-stimulus` output into a real UVM sequence (it proposes stimulus in plain English/register terms; turning that into runnable sequence code is a separate step).
- Multi-level hierarchy tracing: if a tie-off is declared in a sub-module and reaches a top-level port only via a plain wire pass-through (`.PSLVERR(PSLVERR)` at the instantiation, no logic in between), the scanner won't trace through — it only looks in the exact file whose module matches the toggle-report instance's own name. Confirmed real via pulp-platform's `apb_spi_master.sv` (see `../SOC_VERIF`): its real tie-off lives in `spi_master_apb_if.sv`, one level down. Falls through to `unexplained_gap`, not a wrong guess.
- Tie-offs driven by procedural logic (`always_comb`/`always @(*)`) — even an unconditional one — aren't recognized, only plain `assign`. Confirmed real via `apb_timer.sv`'s top-level `PREADY`/`PSLVERR`, which turned out to be genuinely conditional (an `if/else` on an internal mux select) once actually read — a case where the scanner's inability to parse procedural assigns protected against a wrong "tie-off" verdict, not just a missed one.
