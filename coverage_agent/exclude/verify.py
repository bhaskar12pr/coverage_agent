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
    exclusions: list[tuple[str, str]],
    bins: list[ToggleBin],
    rtl: RtlFacts,
    params: dict,
) -> list[VerificationResult]:
    """`exclusions` is a list of (instance, signal) pairs someone has
    already decided to exclude — from a review JSON or a real
    exclusion file. Re-derives ground truth from `bins`/`rtl`/`params`
    (the same inputs suggest_excludes() would use) and checks each one."""
    # A row urg printed as "Excluded" is excluded *because of* the file
    # we're auditing — its real hit status is hidden. Audit it as an
    # untested gap (the question is whether RTL justifies excluding it),
    # not as covered. "Unreachable" (VCS-proven) stays waived.
    bins = [_unexclude(b) for b in bins]
    ground_truth = {(c.instance, c.signal): c for c in suggest_excludes(bins, rtl, params)}
    bin_by_key = {(b.instance, b.signal): b for b in bins}

    results: list[VerificationResult] = []
    rows_by_instance: dict[str, list[ToggleBin]] = {}
    for b in bins:
        rows_by_instance.setdefault(b.instance, []).append(b)

    def _resolve(instance: str, signal: str) -> tuple[str, str] | None:
        """Map an exclusion to the report row it names. Exact match first;
        otherwise real .el naming ("x" / "x[39]") against urg's collapsed
        bit-range rows ("x[0:0]", "x[63:32]"). Prefers a row that is
        still a gap, since that's the one the exclusion matters for."""
        if (instance, signal) in bin_by_key:
            return (instance, signal)
        base, bit = (signal[: signal.rfind("[")], signal[signal.rfind("[") + 1 : -1]) if signal.endswith("]") else (signal, "")
        entry = ElEntry(metric="toggle", scope_kind="instance", scope=instance, signal=base, bit=bit)
        hits = [b for b in rows_by_instance.get(instance, ()) if entry_covers_row(entry, b.instance, b.signal, b.module)]
        if not hits and bit:  # "x[3]" may also be a plain whole-signal name with an index
            entry = ElEntry(metric="toggle", scope_kind="instance", scope=instance, signal=signal)
            hits = [b for b in rows_by_instance.get(instance, ()) if entry_covers_row(entry, b.instance, b.signal, b.module)]
        if not hits:
            return None
        hits.sort(key=lambda b: (b.instance, b.signal) not in ground_truth)
        return (hits[0].instance, hits[0].signal)

    for instance, signal in exclusions:
        key = _resolve(instance, signal)

        if key is None:
            results.append(
                VerificationResult(
                    instance, signal, UNKNOWN_SIGNAL,
                    "not found in the given toggle report for this instance — check for a typo, a "
                    "wrong instance path, or a report that doesn't cover this scope",
                )
            )
            continue

        candidate = ground_truth.get(key)  # key is the matched report row
        if candidate is None:
            # In the report but not in any gap category => fully covered.
            results.append(
                VerificationResult(
                    instance, signal, REDUNDANT,
                    "already fully covered in this report — the exclusion has no effect (may be stale)",
                )
            )
            continue

        if candidate.disposition == SUGGESTED_EXCLUDE:
            results.append(
                VerificationResult(instance, signal, CONFIRMED, f"RTL scanner agrees: {candidate.reason}")
            )
        elif candidate.disposition == LLM_SUGGESTED_EXCLUDE:
            results.append(
                VerificationResult(
                    instance, signal, LLM_ONLY,
                    f"only an LLM opinion backs this, no deterministic RTL match — re-review: {candidate.reason}",
                )
            )
        else:  # UNEXPLAINED_GAP
            results.append(
                VerificationResult(
                    instance, signal, SUSPICIOUS,
                    f"RTL scanner finds NO justification for excluding this — looks like a real, "
                    f"untested gap: {candidate.reason}",
                )
            )

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
        lines.append(f"\nCONFIRMED ({len(by_verdict[CONFIRMED])}) — RTL scanner independently agrees, no action needed.")

    return "\n".join(lines)
