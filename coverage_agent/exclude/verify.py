"""Verify a set of already-decided exclusions against RTL + toggle
report ground truth. This exists because a review JSON's "approved"
flag and a real exclusion file are both, in the end, a human decision
— and humans (or a stale/copy-pasted exclusion list) get it wrong.
This independently re-derives what SHOULD be excluded (via the exact
same RTL scanner suggest_excludes() uses) and cross-checks every
exclusion under review against it.

No LLM involved — this is pure RTL-scanner reuse, so it stays
stdlib-only like the rest of the base tool.
"""

from dataclasses import dataclass, replace

from coverage_agent.exclude.candidates import LLM_SUGGESTED_EXCLUDE, SUGGESTED_EXCLUDE, suggest_excludes
from coverage_agent.exclude.el_parser import ElEntry, entry_covers_row
from coverage_agent.model import EXCLUDED, NOT_COVERED, ToggleBin
from coverage_agent.rtl.scanner import RtlFacts

CONFIRMED = "confirmed"              # RTL scanner independently agrees: dead-by-design
LLM_ONLY = "llm_only"                # only an LLM opinion backs this, not a deterministic RTL match
SUSPICIOUS = "suspicious"            # RTL scanner finds this to be a REAL gap — exclusion looks wrong
REDUNDANT = "redundant"              # signal is already fully covered — exclusion has no effect
UNKNOWN_SIGNAL = "unknown_signal"    # instance/signal not found anywhere in the given toggle report

_NEEDS_ATTENTION = (SUSPICIOUS, UNKNOWN_SIGNAL)


@dataclass
class VerificationResult:
    instance: str
    signal: str
    verdict: str
    detail: str


def _unexclude(b: ToggleBin) -> ToggleBin:
    if not b.excluded:
        return b
    s01 = NOT_COVERED if b.status_0_to_1 == EXCLUDED else b.status_0_to_1
    s10 = NOT_COVERED if b.status_1_to_0 == EXCLUDED else b.status_1_to_0
    return replace(b, status_0_to_1=s01, status_1_to_0=s10)


def verify_exclusions(
    exclusions: list[tuple],
    bins: list[ToggleBin],
    rtl: RtlFacts,
    params: dict,
    const=None,
) -> list[VerificationResult]:
    """`exclusions` is a list of (instance, signal) pairs someone has
    already decided to exclude — from a review JSON or a real exclusion
    file — or (scope, signal, scope_kind) triples, where scope_kind
    "module" means the entry applies to every instance of module `scope`
    (a `MODULE:` block in a real .el file). Re-derives ground truth from
    `bins`/`rtl`/`params` (the same inputs suggest_excludes() would use)
    and checks each one.

    A whole-signal exclusion can cover several report rows (urg splits
    a signal into bit ranges by status, and a module entry covers every
    instance). Every covered row that is still a gap must be justified:
    the verdict is the most concerning one across those rows."""
    # A row urg printed as "Excluded" is excluded *because of* the file
    # we're auditing — its real hit status is hidden. Audit it as an
    # untested gap (the question is whether RTL justifies excluding it),
    # not as covered. "Unreachable" (VCS-proven) stays waived.
    bins = [_unexclude(b) for b in bins]
    ground_truth = {(c.instance, c.signal): c for c in suggest_excludes(bins, rtl, params, const=const)}
    bin_by_key = {(b.instance, b.signal): b for b in bins}

    rows_by_instance: dict[str, list[ToggleBin]] = {}
    rows_by_module: dict[str, list[ToggleBin]] = {}
    for b in bins:
        rows_by_instance.setdefault(b.instance, []).append(b)
        if b.module:
            rows_by_module.setdefault(b.module, []).append(b)

    def _resolve(scope: str, signal: str, scope_kind: str) -> list[ToggleBin]:
        """Every report row an exclusion names. Exact match first;
        otherwise real .el naming ("x" / "x[39]") against urg's
        collapsed bit-range rows ("x[0:0]", "x[63:32]")."""
        if scope_kind == "instance" and (scope, signal) in bin_by_key:
            return [bin_by_key[(scope, signal)]]
        pool = rows_by_module.get(scope, []) if scope_kind == "module" else rows_by_instance.get(scope, [])
        if signal.endswith("]") and "[" in signal:
            base, bit = signal[: signal.rfind("[")], signal[signal.rfind("[") + 1 : -1]
        else:
            base, bit = signal, ""
        entry = ElEntry(metric="toggle", scope_kind=scope_kind, scope=scope, signal=base, bit=bit)
        hits = [b for b in pool if entry_covers_row(entry, b.instance, b.signal, b.module)]
        if not hits and bit:  # "x[3]" may also be a plain whole-signal name with an index
            entry = ElEntry(metric="toggle", scope_kind=scope_kind, scope=scope, signal=signal)
            hits = [b for b in pool if entry_covers_row(entry, b.instance, b.signal, b.module)]
        return hits

    # most concerning first
    severity = {SUSPICIOUS: 0, LLM_ONLY: 1, CONFIRMED: 2}

    results: list[VerificationResult] = []
    for item in exclusions:
        scope, signal = item[0], item[1]
        scope_kind = item[2] if len(item) > 2 else "instance"
        label = f"module:{scope}" if scope_kind == "module" else scope
        rows = _resolve(scope, signal, scope_kind)

        if not rows:
            results.append(
                VerificationResult(
                    label, signal, UNKNOWN_SIGNAL,
                    "not found in the given toggle report for this "
                    + ("module" if scope_kind == "module" else "instance")
                    + " — check for a typo, a wrong instance path, or a report that doesn't cover this scope",
                )
            )
            continue

        gap_rows = [(b, ground_truth[(b.instance, b.signal)]) for b in rows if (b.instance, b.signal) in ground_truth]
        if not gap_rows:
            # In the report but not in any gap category => fully covered.
            results.append(
                VerificationResult(
                    label, signal, REDUNDANT,
                    "already fully covered in this report — the exclusion has no effect (may be stale)",
                )
            )
            continue

        worst = None
        worst_rank = 99
        for b, cand in gap_rows:
            if cand.disposition == SUGGESTED_EXCLUDE:
                v = CONFIRMED
            elif cand.disposition == LLM_SUGGESTED_EXCLUDE:
                v = LLM_ONLY
            else:  # UNEXPLAINED_GAP
                v = SUSPICIOUS
            if severity[v] < worst_rank:
                worst, worst_rank = (v, b, cand), severity[v]
        v, b, cand = worst
        where = "" if len(gap_rows) == 1 else f" [{b.full_name}, 1 of {len(gap_rows)} gap rows this exclusion covers]"
        if v == CONFIRMED:
            reason = f"RTL agrees: {cand.reason}" + (
                "" if len(gap_rows) == 1 else f" (all {len(gap_rows)} covered gap rows justified)"
            )
        elif v == LLM_ONLY:
            reason = f"only an LLM opinion backs this, no deterministic RTL match — re-review: {cand.reason}{where}"
        else:
            reason = (
                "RTL gives NO justification for excluding this — looks like a real, "
                f"untested gap: {cand.reason}{where}"
            )
        results.append(VerificationResult(label, signal, v, reason))

    return results


def format_verification_console(results: list[VerificationResult]) -> str:
    by_verdict: dict[str, list[VerificationResult]] = {}
    for r in results:
        by_verdict.setdefault(r.verdict, []).append(r)

    n_needs_attention = sum(len(by_verdict.get(v, [])) for v in _NEEDS_ATTENTION)
    lines = [f"Verified {len(results)} exclusion(s) — {n_needs_attention} need attention"]

    if by_verdict.get(SUSPICIOUS):
        items = by_verdict[SUSPICIOUS]
        lines.append(f"\nSUSPICIOUS ({len(items)}) — RTL does not justify these, likely added incorrectly:")
        for r in items:
            lines.append(f"  {r.instance}.{r.signal}")
            lines.append(f"      {r.detail}")

    if by_verdict.get(UNKNOWN_SIGNAL):
        items = by_verdict[UNKNOWN_SIGNAL]
        lines.append(f"\nUNKNOWN SIGNAL ({len(items)}) — not found in the given report:")
        for r in items:
            lines.append(f"  {r.instance}.{r.signal}")
            lines.append(f"      {r.detail}")

    if by_verdict.get(LLM_ONLY):
        items = by_verdict[LLM_ONLY]
        lines.append(f"\nLLM-ONLY ({len(items)}) — no deterministic RTL match, worth a second look:")
        for r in items:
            lines.append(f"  {r.instance}.{r.signal}")
            lines.append(f"      {r.detail}")

    if by_verdict.get(REDUNDANT):
        items = by_verdict[REDUNDANT]
        lines.append(f"\nREDUNDANT ({len(items)}) — already fully covered, exclusion has no effect:")
        for r in items:
            lines.append(f"  {r.instance}.{r.signal}")

    if by_verdict.get(CONFIRMED):
        lines.append(f"\nCONFIRMED ({len(by_verdict[CONFIRMED])}) — RTL independently agrees, no action needed.")

    return "\n".join(lines)
