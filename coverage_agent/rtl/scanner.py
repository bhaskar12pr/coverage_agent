"""Heuristic Verilog/SystemVerilog scanner for exclude-generation.

This is NOT a Verilog parser — it's regex-based pattern matching for
exactly two dead-code shapes:

  1. Constant tie-offs:      assign <name>[<hi>:<lo>] = <const-expr>;
  2. Parameter-gated blocks: generate if (<cond>) begin ... end
                              else begin ... end endgenerate

It will miss patterns it doesn't recognize (nested generates, case
generate, tie-offs via always blocks, SystemVerilog always_comb, etc.)
— a miss just means a signal falls through to "unexplained gap",
which is the safe failure mode (never silently over-excludes).

Validate findings against your actual RTL style; extend the regexes
here as needed (see README.md).
"""

import re
from dataclasses import dataclass

_COMMENT_LINE_RE = re.compile(r"//.*")
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_PARAM_RE = re.compile(r"\bparameter\s+(?:\w+\s+)?(\w+)\s*=\s*(-?\d+)")

_ASSIGN_RE = re.compile(
    r"\bassign\s+(?P<name>\w+)\s*(?:\[\s*(?P<range>[^\]]+?)\s*\])?\s*=\s*(?P<rhs>[^;]+);",
    re.MULTILINE,
)

_GENERATE_IF_ELSE_RE = re.compile(
    r"\bif\s*\(\s*(?P<cond>[^)]+?)\s*\)\s*begin\b[^\n]*"
    r"(?P<true_body>.*?)"
    r"\bend\s+else\s+begin\b[^\n]*"
    r"(?P<false_body>.*?)"
    r"\bend\b",
    re.DOTALL,
)

_DRIVEN_SIGNAL_RE = re.compile(r"\b(\w+)\s*(?:<=|=(?!=))")

_VERILOG_KEYWORDS = {
    "if", "else", "begin", "end", "posedge", "negedge", "or", "and",
    "always", "assign", "reg", "wire", "generate", "endgenerate",
    "case", "endcase", "default",
}

_CONST_TOKEN_RE = re.compile(
    r"[A-Za-z_]\w*|\d+'[bBhHdDoO][0-9a-fA-Fxz_]+|\d+|[{}()+\-*,]"
)


@dataclass(frozen=True)
class TieOff:
    signal: str
    range_text: str | None  # raw text, e.g. "31:NUM_GPIO" — evaluated per-config, not per-RTL-default
    source: str

    def resolve(self, params: dict) -> tuple[int, int] | None:
        """Resolve this tie-off's bit range against a specific
        derivative's parameter values. Returns None if the range
        can't be evaluated (unsupported expression / missing param)."""
        from coverage_agent.rtl.condeval import UnsupportedExpression, eval_int

        if self.range_text is None:
            return None
        try:
            if ":" not in self.range_text:
                v = eval_int(self.range_text, params)
                return v, v
            hi_text, lo_text = self.range_text.split(":", 1)
            return eval_int(hi_text, params), eval_int(lo_text, params)
        except UnsupportedExpression:
            return None

    def contains_bit(self, bit: int | None, params: dict) -> bool:
        if self.range_text is None:
            return bit is None
        if bit is None:
            return False
        resolved = self.resolve(params)
        if resolved is None:
            return False
        hi, lo = resolved
        return lo <= bit <= hi


@dataclass(frozen=True)
class GenerateGate:
    signal: str
    condition: str
    # Whether the branch's driver for this signal is a constant
    # (tie-off) — None means the signal is driven in that branch but
    # not via a plain `assign ... = <const>;` we can classify (e.g. a
    # clocked reg), so we can't conclude it's dead there.
    true_is_tied: bool | None
    false_is_tied: bool | None
    source: str


@dataclass
class RtlFacts:
    source_files: list[str]
    tie_offs: list[TieOff]
    generate_gates: list[GenerateGate]
    params: dict[str, int]


def _strip_comments(text: str) -> str:
    text = _COMMENT_BLOCK_RE.sub(" ", text)
    text = _COMMENT_LINE_RE.sub("", text)
    return text


def _is_constant_rhs(rhs: str, known_params: set[str]) -> bool:
    tokens = _CONST_TOKEN_RE.findall(rhs)
    if not tokens:
        return False
    for tok in tokens:
        if tok in ("{", "}", "(", ")", "+", "-", "*", ","):
            continue
        if re.match(r"^\d+'[bBhHdDoO][0-9a-fA-Fxz_]+$", tok):
            continue
        if re.match(r"^\d+$", tok):
            continue
        if tok in known_params:
            continue
        return False
    return True


def _scan_tie_offs(text: str, known_params: set[str], source: str) -> list[TieOff]:
    """Only unconditional `assign`s — text inside generate blocks is
    blanked out first, since a signal driven differently per branch
    (the common if/else shape) is NOT an unconditional tie-off; it's
    handled by _scan_generate_gates instead."""
    without_generates = re.sub(r"\bgenerate\b.*?\bendgenerate\b", " ", text, flags=re.DOTALL)
    tie_offs = []
    for m in _ASSIGN_RE.finditer(without_generates):
        rhs = m.group("rhs").strip()
        if not _is_constant_rhs(rhs, known_params):
            continue
        tie_offs.append(TieOff(signal=m.group("name"), range_text=m.group("range"), source=source))
    return tie_offs


def _driven_signals(body: str) -> set[str]:
    names = set()
    for m in _DRIVEN_SIGNAL_RE.finditer(body):
        name = m.group(1)
        if name not in _VERILOG_KEYWORDS:
            names.add(name)
    return names


def _branch_tie_status(body: str, signal: str, known_params: set[str]) -> bool | None:
    """Is `signal` driven in this branch, and if so, via a plain
    `assign <signal> = <const>;`? Returns True/False if classifiable,
    None if the signal isn't driven here via a pattern we recognize
    (e.g. a clocked reg in an always block) — that's "can't tell",
    not "not tied"."""
    found_driven = signal in _driven_signals(body)
    for m in _ASSIGN_RE.finditer(body):
        if m.group("name") != signal:
            continue
        return _is_constant_rhs(m.group("rhs").strip(), known_params)
    return None if found_driven else None


def _scan_generate_gates(text: str, known_params: set[str], source: str) -> list[GenerateGate]:
    gates = []
    for gm in re.finditer(r"\bgenerate\b(.*?)\bendgenerate\b", text, re.DOTALL):
        block = gm.group(1)
        m = _GENERATE_IF_ELSE_RE.search(block)
        if not m:
            continue
        cond = m.group("cond").strip()
        true_body = m.group("true_body")
        false_body = m.group("false_body")
        signals = _driven_signals(true_body) | _driven_signals(false_body)
        for sig in signals:
            gates.append(
                GenerateGate(
                    signal=sig,
                    condition=cond,
                    true_is_tied=_branch_tie_status(true_body, sig, known_params),
                    false_is_tied=_branch_tie_status(false_body, sig, known_params),
                    source=source,
                )
            )
    return gates


def scan_rtl_text(text: str, source: str = "<string>") -> RtlFacts:
    clean = _strip_comments(text)
    params = {name: int(val) for name, val in _PARAM_RE.findall(clean)}
    known_params = set(params.keys())
    tie_offs = _scan_tie_offs(clean, known_params, source)
    gates = _scan_generate_gates(clean, known_params, source)
    return RtlFacts(source_files=[source], tie_offs=tie_offs, generate_gates=gates, params=params)


def scan_rtl_files(paths: list[str]) -> RtlFacts:
    all_tie_offs: list[TieOff] = []
    all_gates: list[GenerateGate] = []
    all_params: dict[str, int] = {}
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        facts = scan_rtl_text(text, source=path)
        all_tie_offs.extend(facts.tie_offs)
        all_gates.extend(facts.generate_gates)
        all_params.update(facts.params)
    return RtlFacts(source_files=list(paths), tie_offs=all_tie_offs, generate_gates=all_gates, params=all_params)
