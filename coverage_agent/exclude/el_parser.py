"""Parsers for VCS coverage exclusion files.

1. Real VCS `.el` files (urg/Verdi "Format Version: 2"), as loaded with
   `urg -elfile`. Validated against the OpenTitan top_earlgrey exclusion
   files (hw/top_earlgrey/dv/cov/*.el) and the matching chip-level urg
   report — see tests/test_el_parser.py. Shape:

       CHECKSUM: "1202717726 1014860158"
       INSTANCE: tb.dut.top_earlgrey            (or MODULE: name ( parameter ... ))
       ANNOTATION: "[UNR] Tie offs."
       Toggle mio_attr_o[9].slew_rate "logic mio_attr_o[9].slew_rate[1:0]"
       Toggle app_i[2].data [39] "logic app_i[2].data[63:0]"     <- one bit
       Toggle 1to0 sig "logic sig"                             <- one direction
       Condition / Branch / Block / Fsm ...                    <- other metrics

   ANNOTATION applies to the next exclusion line only. CHECKSUM is
   computed by VCS from the module; it can't be recreated here.

2. `-node` toggle exclusions in a coverage hierarchy config (`-cm_hier`
   .cfg), as OpenTitan uses for clkmgr/pwrmgr:

       -node tb.dut*.u_clkmgr.cg_en_o.aon_powerup
       -node tb.dut.top_earlgrey.u_spi_host1 passthrough_i.*

3. The legacy coverage_agent-generated shape (`Exclude Toggle "sig"`),
   kept so older generated files can still be audited.
"""

import re
from dataclasses import dataclass

_SCOPE_RE = re.compile(r"^(?P<kind>INSTANCE|MODULE):\s*(?P<name>.+?)\s*$")
_ANNOTATION_RE = re.compile(r'^ANNOTATION:\s*"(?P<text>.*)"\s*$')
_TOGGLE_RE = re.compile(
    r"^Toggle\s+"
    r"(?:(?P<dir>0to1|1to0)\s+)?"
    r"(?P<sig>\S+)"
    r"(?:\s+\[(?P<bit>\d+(?::\d+)?)\])?"
    r'(?:\s+"(?P<decl>[^"]*)")?\s*$'
)
_LEGACY_EXCLUDE_RE = re.compile(r'^Exclude\s+Toggle\s+"(?P<sig>[^"]+)"')
_NODE_RE = re.compile(r"^-node\s+(?P<a>\S+)(?:\s+(?P<b>\S+))?\s*$")
_METRIC_WORDS = ("Toggle", "Condition", "Branch", "Block", "Fsm", "Assert", "Line", "Cond")


@dataclass(frozen=True)
class ElEntry:
    metric: str            # "toggle", "condition", "branch", "block", ...
    scope_kind: str        # "instance" or "module"
    scope: str             # instance path, or module name (params stripped)
    signal: str = ""       # toggle only: signal as written, e.g. "app_i[2].data"
    bit: str = ""          # toggle only: "39" for a single-bit exclusion, "" = whole signal
    direction: str = ""    # toggle only: "0to1" / "1to0", "" = both
    decl: str = ""         # toggle only: declaration VCS recorded, e.g. "logic x[63:0]"
    annotation: str = ""   # ANNOTATION text, e.g. "[UNR] Tie offs."
    module_params: str = ""
    line: int = 0

    @property
    def signal_with_bit(self) -> str:
        return f"{self.signal}[{self.bit}]" if self.bit else self.signal


def parse_vcs_el(text: str) -> list[ElEntry]:
    entries: list[ElEntry] = []
    scope_kind, scope, params = "instance", "", ""
    annotation = ""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("CHECKSUM:"):
            continue
        m = _SCOPE_RE.match(line)
        if m:
            scope_kind = m.group("kind").lower()
            name = m.group("name")
            params = ""
            if scope_kind == "module" and "(" in name:
                name, _, params = name.partition("(")
                name, params = name.strip(), params.rstrip(")").strip()
            scope = name
            annotation = ""
            continue
        m = _ANNOTATION_RE.match(line)
        if m:
            annotation = m.group("text")
            continue
        m = _TOGGLE_RE.match(line)
        if m:
            entries.append(
                ElEntry(
                    metric="toggle",
                    scope_kind=scope_kind,
                    scope=scope,
                    signal=m.group("sig"),
                    bit=m.group("bit") or "",
                    direction=m.group("dir") or "",
                    decl=m.group("decl") or "",
                    annotation=annotation,
                    module_params=params,
                    line=lineno,
                )
            )
            annotation = ""
            continue
        word = line.split(None, 1)[0]
        if word in _METRIC_WORDS:
            entries.append(
                ElEntry(metric=word.lower(), scope_kind=scope_kind, scope=scope,
                        annotation=annotation, module_params=params, line=lineno)
            )
            annotation = ""
    return entries


@dataclass(frozen=True)
class NodeExclusion:
    """A `-node` toggle exclusion from a -cm_hier config. `pattern` is a
    glob over the full hierarchical signal path (`*` wildcards)."""

    pattern: str
    line: int

    def matches(self, full_name: str) -> bool:
        rx = "^" + re.escape(self.pattern).replace(r"\*", ".*") + r"(?:\[[\d:]+\])?$"
        return re.match(rx, full_name) is not None


def parse_cov_cfg_nodes(text: str) -> list[NodeExclusion]:
    out: list[NodeExclusion] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("//", 1)[0].strip()
        m = _NODE_RE.match(line)
        if not m:
            continue
        a, b = m.group("a"), m.group("b")
        out.append(NodeExclusion(pattern=f"{a}.{b}" if b else a, line=lineno))
    return out


def parse_ucm_exclusion_file(text: str) -> list[tuple[str, str]]:
    """Returns [(instance, signal), ...] toggle exclusions in file order,
    from either a real VCS .el file or the legacy generated shape.
    MODULE-scoped entries are returned with the module name as instance."""
    if not re.search(r'^\s*Exclude\s+Toggle\s+"', text, re.MULTILINE):
        return [(e.scope, e.signal_with_bit) for e in parse_vcs_el(text) if e.metric == "toggle"]

    pairs: list[tuple[str, str]] = []
    current_instance = ""
    for line in text.splitlines():
        m = _SCOPE_RE.match(line.strip())
        if m:
            current_instance = m.group("name")
            continue
        m = _LEGACY_EXCLUDE_RE.match(line.strip())
        if m:
            pairs.append((current_instance, m.group("sig")))
    return pairs


_TRAILING_RANGE_RE = re.compile(r"\[(?P<hi>\d+)(?::(?P<lo>\d+))?\]$")


def _split_range(sig: str) -> tuple[str, tuple[int, int] | None]:
    m = _TRAILING_RANGE_RE.search(sig)
    if not m:
        return sig, None
    hi = int(m.group("hi"))
    lo = int(m.group("lo")) if m.group("lo") is not None else hi
    return sig[: m.start()], (min(hi, lo), max(hi, lo))


def entry_covers_row(entry: ElEntry, instance: str, signal: str, module: str = "") -> bool:
    """Does a toggle .el entry apply to one urg report row?

    Report rows carry the bit range urg collapsed them into
    ("dio_attr_i[0].pull_en[0:0]", "app_i[2].data[63:39]"); .el entries
    name the whole signal ("dio_attr_i[0].pull_en") or one bit
    ("app_i[2].data [39]"). A whole-signal entry covers every row of
    that signal; a bit entry covers the row whose range contains it.
    """
    if entry.metric != "toggle":
        return False
    if entry.scope_kind == "instance" and entry.scope != instance:
        return False
    if entry.scope_kind == "module" and entry.scope != module:
        return False
    if signal == entry.signal_with_bit:
        return True
    base, rng = _split_range(signal)
    if not entry.bit:
        return base == entry.signal or signal == entry.signal
    if base != entry.signal or rng is None:
        return False
    lo_s, _, hi_s = entry.bit.partition(":")
    lo, hi = sorted((int(lo_s), int(hi_s or lo_s)))
    return rng[0] <= lo and hi <= rng[1]
