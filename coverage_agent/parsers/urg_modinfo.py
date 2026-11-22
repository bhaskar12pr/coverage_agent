"""Parser for real VCS urg `-format text` reports (modinfo.txt).

Validated against real urg output (VCS T-2022.06-SP2) published by the
OpenTitan project at reports.opentitan.org — the rv_dm and gpio IP
regressions and the top_earlgrey chip-level regression. See
tests/test_urg_modinfo.py and samples/urg_real/.

modinfo.txt interleaves every metric (line, cond, toggle, FSM, branch,
assert) for every module and instance. Only these sections are read:

    Toggle Coverage for Instance : tb.dut.u_padring(x)
    Toggle Coverage for Module : gpio
                    Total Covered Percent
    Totals          399   385     96.49
    Total Bits      1458  1408    96.57
    Total Bits 0->1 729   713     97.81
    Total Bits 1->0 729   695     95.34
    ...
    Port Details
                       Toggle Toggle 1->0 Tests    Toggle 0->1 Tests    Direction Exclude Annotation
    dio_in_raw_o[8:3]  No     No                   Yes         T21,T49  OUTPUT
    dio_attr_i[0].pull_en[0:0] Excluded Excluded   Excluded             INPUT     [UNR] Tie offs.
    -------------------------------------------------------------------------------

Row shape, which this parser relies on (not on column positions):
    <name> <overall> <1->0> [tests] <0->1> [tests] [direction] [annotation...]
Status words: Yes / No / Unreachable / Excluded. Tests: comma-separated
"T<n>" ids, "*" prefix = covers at least one bit of the range. Tests
columns are absent when urg runs without `-show tests`.

A trailing "(x)" on an instance name marks an instance with exclusions
applied; it is stripped.

urg prints a Module section (merged over all instances of that module)
and, for some modules, per-Instance sections. To avoid double counting,
a module section is dropped when every known instance of that module
has its own instance section; otherwise it is kept, resolved to the
instance path when the module has exactly one instance (see
`ModinfoResult.bins`).
"""

import re
from dataclasses import dataclass, field

from coverage_agent.model import COVERED, EXCLUDED, NOT_COVERED, UNREACHABLE, ToggleBin

_SECTION_RE = re.compile(r"^Toggle Coverage for (?P<kind>Instance|Module)\s*:\s*(?P<name>.+?)\s*$")
_SEPARATOR_RE = re.compile(r"^-{20,}\s*$")
_DETAILS_RE = re.compile(r"^(?P<what>\w+) Details\s*$")
_TOTAL_RE = re.compile(r"^Total Bits (?P<dir>0->1|1->0)\s+(?P<total>\d+)\s+(?P<covered>\d+)")
_MOD_INST_RE = re.compile(r"^Module Instance\s*:\s*(?P<inst>\S+)")
_TESTS_RE = re.compile(r"^\*?T\d+(?:,\*?T\d+)*$")

_STATUS_WORDS = {
    "yes": COVERED,
    "no": NOT_COVERED,
    "unreachable": UNREACHABLE,
    "excluded": EXCLUDED,
}
_DIRECTIONS = {"INPUT", "OUTPUT", "INOUT"}


_SECTION_ANYWHERE_RE = re.compile(r"^Toggle Coverage for (?:Instance|Module)\s*:", re.MULTILINE)


def looks_like_modinfo(text: str) -> bool:
    return _SECTION_ANYWHERE_RE.search(text) is not None


@dataclass
class ToggleSection:
    kind: str                     # "instance" or "module"
    name: str                     # instance path or module name, "(x)" stripped
    has_exclusions: bool          # instance name carried the "(x)" marker
    total_bits_0_to_1: int | None = None
    covered_bits_0_to_1: int | None = None
    total_bits_1_to_0: int | None = None
    covered_bits_1_to_0: int | None = None
    bins: list[ToggleBin] = field(default_factory=list)
    unparsed_rows: list[str] = field(default_factory=list)


@dataclass
class ModinfoResult:
    sections: list[ToggleSection]
    instance_module: dict[str, str]   # instance path -> module name, from "Module Instance" blocks

    def module_instances(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for inst, mod in self.instance_module.items():
            out.setdefault(mod, []).append(inst)
        return out

    @property
    def bins(self) -> list[ToggleBin]:
        """Deduplicated bins: instance sections, plus module sections
        that aren't fully represented by instance sections."""
        inst_sections = {s.name for s in self.sections if s.kind == "instance"}
        by_module = self.module_instances()
        out: list[ToggleBin] = []
        for s in self.sections:
            if s.kind == "instance":
                mod = self.instance_module.get(s.name, "")
                out.extend(_with(b, module=mod) for b in s.bins)
                continue
            insts = by_module.get(s.name, [])
            if insts and all(i in inst_sections for i in insts):
                continue  # fully covered by per-instance sections
            if len(insts) == 1:
                out.extend(_with(b, instance=insts[0], module=s.name, scope_kind="instance") for b in s.bins)
            else:
                out.extend(_with(b, module=s.name) for b in s.bins)
        return out


def _with(b: ToggleBin, **changes) -> ToggleBin:
    from dataclasses import replace

    return replace(b, **changes)


def _strip_x(name: str) -> tuple[str, bool]:
    """urg appends "(x)" to instances that have exclusions applied."""
    if name.endswith("(x)"):
        return name[: -len("(x)")], True
    return name, False


def _parse_row(line: str, scope: str, scope_kind: str) -> ToggleBin | None:
    tokens = line.split()
    if len(tokens) < 4:
        return None
    name = tokens[0]
    status_idx = [i for i, t in enumerate(tokens[1:], start=1) if t.lower() in _STATUS_WORDS]
    # need overall, 1->0, 0->1 — the first three status words
    if len(status_idx) < 3 or status_idx[0] != 1 or status_idx[1] != 2:
        return None
    i_overall, i_10, i_01 = status_idx[:3]
    tests_10 = tokens[i_10 + 1 : i_01]
    if any(not _TESTS_RE.match(t) for t in tests_10):
        return None
    rest = tokens[i_01 + 1 :]
    tests_01: list[str] = []
    if rest and _TESTS_RE.match(rest[0]):
        tests_01 = [rest[0]]
        rest = rest[1:]
    direction = ""
    if rest and rest[0] in _DIRECTIONS:
        direction = rest[0]
        rest = rest[1:]
    annotation = " ".join(rest)

    s10 = _STATUS_WORDS[tokens[i_10].lower()]
    s01 = _STATUS_WORDS[tokens[i_01].lower()]

    def _split_tests(cells: list[str]) -> tuple[str, ...]:
        return tuple(t for cell in cells for t in cell.split(",") if t)

    return ToggleBin(
        instance=scope,
        signal=name,
        hit_0_to_1=s01 == COVERED,
        hit_1_to_0=s10 == COVERED,
        status_0_to_1=s01,
        status_1_to_0=s10,
        direction=direction,
        tests_0_to_1=_split_tests(tests_01),
        tests_1_to_0=_split_tests(tests_10),
        annotation=annotation,
        scope_kind=scope_kind,
    )


def parse_urg_modinfo(text: str) -> ModinfoResult:
    sections: list[ToggleSection] = []
    instance_module: dict[str, str] = {}

    current: ToggleSection | None = None
    in_table = False          # inside a "<X> Details" table, past its header row
    expect_header = False     # just saw "<X> Details", next non-blank line is the column header

    mod_inst: str | None = None   # inside a "Module Instance : X" block
    mod_inst_state = 0            # 0 idle, 1 saw "Module :", 2 saw its NAME header

    for line in text.splitlines():
        # --- instance -> module map from "Module Instance" summary blocks ---
        m = _MOD_INST_RE.match(line)
        if m:
            mod_inst, mod_inst_state = _strip_x(m.group("inst"))[0], 0
            continue
        if mod_inst is not None and current is None:
            stripped = line.strip()
            if mod_inst_state == 0 and stripped == "Module :":
                mod_inst_state = 1
            elif mod_inst_state == 1 and stripped.startswith("SCORE") and stripped.endswith("NAME"):
                mod_inst_state = 2
            elif mod_inst_state == 2 and stripped:
                instance_module[mod_inst] = stripped.split()[-1]
                mod_inst, mod_inst_state = None, 0
            continue

        # --- toggle sections ---
        m = _SECTION_RE.match(line)
        if m:
            name, has_x = _strip_x(m.group("name"))
            current = ToggleSection(kind=m.group("kind").lower(), name=name, has_exclusions=has_x)
            sections.append(current)
            in_table = expect_header = False
            continue
        if current is None:
            continue
        if _SEPARATOR_RE.match(line):
            current = None
            in_table = expect_header = False
            continue

        m = _TOTAL_RE.match(line)
        if m and not in_table:
            total, covered = int(m.group("total")), int(m.group("covered"))
            if m.group("dir") == "0->1":
                current.total_bits_0_to_1, current.covered_bits_0_to_1 = total, covered
            else:
                current.total_bits_1_to_0, current.covered_bits_1_to_0 = total, covered
            continue

        if _DETAILS_RE.match(line):
            expect_header, in_table = True, False
            continue
        if expect_header:
            if line.strip():
                expect_header, in_table = False, True  # this is the column header row
            continue
        if in_table:
            stripped = line.strip()
            if not stripped or stripped.startswith("*Tests covering"):
                in_table = False
                continue
            b = _parse_row(line, current.name, current.kind)
            if b is None:
                current.unparsed_rows.append(line)
            else:
                current.bins.append(b)

    return ModinfoResult(sections=sections, instance_module=instance_module)
