# Real VCS urg reports and exclusion files (OpenTitan)

Everything in this directory is **unmodified real tool output or source**
from the [OpenTitan](https://github.com/lowRISC/opentitan) project
(lowRISC contributors, Apache License 2.0 — see `LICENSE.opentitan`),
cut down to small excerpts so the test suite stays fast. It is the
ground truth the `urg_modinfo` and `.el` parsers are validated against.

| Path | Source |
|---|---|
| `opentitan_earlgrey/modinfo.txt` | `reports.opentitan.org/hw/top_earlgrey/dv/latest/cov_report/modinfo.txt` (chip-level regression, 10 Feb 2025, VCS T-2022.06-SP2, 2,930 tests). Excerpt: the `tb.dut.u_padring` "Module Instance" block, its Line section (to prove non-toggle sections are ignored) and its toggle section, unchanged. |
| `opentitan_earlgrey/dashboard.txt` | Same report's dashboard (shows the `-elfile` list used) |
| `opentitan_earlgrey/*.el`, `*.cfg` | `hw/top_earlgrey/dv/cov/` at commit `88b3d484a7278a5b4e04d56a8e07baf630513d96` (Jan 2025, matches the report's hierarchy). `pinmux_pad_attr_padring.el` is the `INSTANCE: tb.dut.u_padring` block of `pinmux_pad_attr.el` (with its header); `chip_cover_tgl.cfg` is the `begin tgl ... end` block of `chip_cover.cfg`. |
| `opentitan_gpio/modinfo.txt` | `reports.opentitan.org/hw/ip/gpio/dv/latest/cov_report/modinfo.txt` (9 Feb 2025): the `Toggle Coverage for Module : gpio` section, unchanged |
| `opentitan_rv_dm/modinfo.txt` | `reports.opentitan.org/hw/ip/rv_dm/dv/2024.07.26_09.09.18/cov_report/modinfo.txt`: two toggle sections, unchanged |

Full-report results (not just these excerpts), from this repo's parser:

| Report | Toggle sections | Parsed bits vs urg section totals | Parsed toggle score | urg dashboard TOGGLE |
|---|---|---|---|---|
| rv_dm | 11 | all match | 89.91% | 89.91% |
| gpio | 5 | all match | 100.00% | 100.00% |
| top_earlgrey | 80 | all match | 95.36% | 95.36% |

All 1,122 `Excluded` toggle rows in the full Earlgrey report are traced
to an entry in the 12 real `.el` files; 25 `.el` entries match nothing
(`top_earlgrey.ram_2p_cfg_i.*` — the port had been renamed
`spi_ram_2p_cfg_i`, so those exclusions are stale and the renamed bits
show up as real gaps).
