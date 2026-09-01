# Source

`apb_gpio.sv` is copied verbatim from
[pulp-platform/apb_gpio](https://github.com/pulp-platform/apb_gpio),
commit [`f82caeb`](https://github.com/pulp-platform/apb_gpio/commit/f82caeb7f7d89427f05e9af5ed31e0675efe0d83),
`rtl/apb_gpio.sv`.

Copyright 2018 ETH Zurich and University of Bologna. Licensed under the
Solderpad Hardware License, Version 0.51 (see `LICENSE` in this
directory) — the license itself permits treating the work as
Apache License 2.0 at the licensee's option.

Pulled in as **real, permissively-licensed RTL** to validate
`coverage_agent`'s RTL scanner against something other than the
synthetic `soc_sample/rtl/apb_gpio.v` this project started with — see
`../configs/pulp_gpio_*.json` and `../../samples/urg_text/pulp_apb_gpio_tgl.txt`.

## Why this one (vs. OpenTitan's GPIO, considered first)

OpenTitan's GPIO IP is generated per chip (earlgrey/darjeeling/...) and
is a genuine real-world "derivative" example, but its register
interface uses struct field references (`hw2reg.data_in.de`) that
coverage_agent's scanner doesn't parse yet (`\w+`-only identifiers).
This file uses flat signal names — `PSLVERR`, `gpio_dir`, etc. — that
match what the scanner already handles, so it's useful for validation
today. Revisit OpenTitan's GPIO once the scanner supports dotted
signal names.

## What the scanner actually finds here

- `assign PSLVERR = 1'b0;` (line 784 as of the above commit) — a clean,
  unconditional tie-off. This is the only signal our scanner
  confidently classifies in this file without further work.
- The `PAD_NUM` parameter (default 32, max 64) gates a second bank of
  registers/interrupt-status bits via **runtime `if (i < PAD_NUM)`
  guards inside `always`/`for` loops** — not a `generate if/else`
  block. coverage_agent's scanner does not yet recognize this pattern,
  so those upper-bank signals correctly fall through to
  `unexplained_gap` (the safe default) rather than being
  misclassified. Worth extending the scanner for later, not required
  to use this file today.
