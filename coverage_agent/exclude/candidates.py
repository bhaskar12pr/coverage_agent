"""Cross-reference toggle coverage gaps against RTL facts to suggest
which gaps are legitimately dead-by-design (tie-off / parameter-gated)
vs. real verification gaps that need a test.

Design rule (see conversation / README): this NEVER auto-approves an
exclusion. Every suggestion is written to a human-reviewable file with
`approved: null`; nothing is emitted into an actual exclusion file
until a human sets `approved: true`. A wrong auto-waiver here hides a
real bug, so silence (falling through to "unexplained_gap") is always
the safe default when a signal can't be confidently classified.
"""

import re
from dataclasses import dataclass, field

from coverage_agent.model import ToggleBin
from coverage_agent.report import build_gap_report
from coverage_agent.rtl.condeval import UnsupportedExpression, eval_bool
from coverage_agent.rtl.scanner import RtlFacts

_SIGNAL_RE = re.compile(r"^(?P<base>\w+)(?:\[(?P<bit>\d+)\])?$")

SUGGESTED_EXCLUDE = "suggested_exclude"
UNEXPLAINED_GAP = "unexplained_gap"
LLM_SUGGESTED_EXCLUDE = "llm_suggested_exclude"  # deterministic-scanner miss, LLM opinion only — see llm/judge.py


@dataclass
class ExcludeCandidate:
    instance: str
    signal: str
    disposition: str
    reason: str
    source: str
    approved: bool | None = field(default=None)
    reviewer: str | None = field(default=None)
    note: str | None = field(default=None)
    # True only for the pure "no tie-off/gate found at all" fallback —
    # never for a partial-toggle gap (structurally can't be a tie-off)
    # or a gap the scanner already resolved as real via a live generate
    # branch. This is the sole set --llm is allowed to touch.
    llm_eligible: bool = field(default=False)
    # "never_toggled" | "missing_0_to_1" | "missing_1_to_0" — which
    # transition(s) the toggle report is actually missing, for callers
    # (e.g. llm/stimulus.py) that need it structured, not parsed out
    # of `reason`.
    gap_kind: str = field(default="never_toggled")

    @property
    def candidate_id(self) -> str:
        return f"{self.instance}::{self.signal}"


def _parse_signal(name: str) -> tuple[str, int | None]:
    m = _SIGNAL_RE.match(name)
    if not m:
        return name, None
    bit = m.group("bit")
    return m.group("base"), (int(bit) if bit is not None else None)


def _candidate_files(instance: str, rtl: RtlFacts) -> set[str] | None:
    """Which scanned file(s) implement this toggle-report instance's
    module, so a same-named signal in a DIFFERENT IP's file (PREADY,
    PSLVERR, ... are extremely common across APB peripherals) can't be
    matched to the wrong instance. Uses the leaf instance name (the
    last '.'-separated segment) against instantiations found while
    scanning — see rtl.instance_to_module. Returns None when the
    instance wasn't found in any instantiation (e.g. a single-IP scan
    with no SoC-integration file in the scan set) — callers should
    fall back to searching every scanned file in that case, the
    pre-instance-aware behavior."""
    leaf = instance.rsplit(".", 1)[-1]
    return rtl.files_for_instance(leaf)


def _classify_never_toggled(bin_: ToggleBin, rtl: RtlFacts, config_params: dict) -> ExcludeCandidate:
    """`config_params` is the active derivative's own param overrides
    ONLY — never a pre-merged dict of every scanned file's RTL
    defaults. Each tie-off/gate is resolved against its OWN source
    file's declared defaults (via rtl.params_for), overridden by
    config_params — this matters once more than one RTL file is
    scanned together, since real IPs commonly reuse generic parameter
    names (BUFFER_DEPTH, TX_FIFO_DEPTH, ...) with different values
    per file/module."""
    base, bit = _parse_signal(bin_.signal)
    candidate_files = _candidate_files(bin_.instance, rtl)

    for tie_off in rtl.tie_offs:
        if candidate_files is not None and tie_off.source not in candidate_files:
            continue
        effective_params = rtl.params_for(tie_off.source, config_params)
        if tie_off.signal == base and tie_off.contains_bit(bit, effective_params):
            where = f"[{bit}]" if bit is not None else ""
            return ExcludeCandidate(
                instance=bin_.instance,
                signal=bin_.signal,
                disposition=SUGGESTED_EXCLUDE,
                reason=f"tie-off: '{base}{where}' is driven by a constant assign, never expected to toggle",
                source=tie_off.source,
            )

    for gate in rtl.generate_gates:
        if gate.signal != base:
            continue
        if candidate_files is not None and gate.source not in candidate_files:
            continue
        effective_params = rtl.params_for(gate.source, config_params)
        try:
            cond_true = eval_bool(gate.condition, effective_params)
        except UnsupportedExpression:
            continue
        active_is_tied = gate.true_is_tied if cond_true else gate.false_is_tied
        active_branch = "true" if cond_true else "false"
        if active_is_tied is True:
            return ExcludeCandidate(
                instance=bin_.instance,
                signal=bin_.signal,
                disposition=SUGGESTED_EXCLUDE,
                reason=(
                    f"parameter-gated tie-off: '{base}' is driven by a constant assign in the "
                    f"active generate branch ({gate.condition} == {cond_true} -> {active_branch} branch)"
                ),
                source=gate.source,
            )
        if active_is_tied is False:
            return ExcludeCandidate(
                instance=bin_.instance,
                signal=bin_.signal,
                disposition=UNEXPLAINED_GAP,
                reason=(
                    f"'{base}' is driven by non-constant logic in the active generate branch "
                    f"({gate.condition} == {cond_true} -> {active_branch} branch) — this config does not "
                    "tie it off, so the toggle gap is real and needs investigation/a test"
                ),
                source=gate.source,
            )
        # active_is_tied is None: signal exists in this branch (e.g. a
        # clocked reg) but not via a pattern we can classify as constant
        # or not — fall through to "unexplained", the safe default.

    return ExcludeCandidate(
        instance=bin_.instance,
        signal=bin_.signal,
        disposition=UNEXPLAINED_GAP,
        reason="no tie-off or parameter-gating found in scanned RTL for this signal — needs a directed test",
        source="",
        llm_eligible=True,
    )


def format_candidates_console(candidates: list[ExcludeCandidate], project: str, ip_version: str) -> str:
    suggested = [c for c in candidates if c.disposition == SUGGESTED_EXCLUDE]
    llm_suggested = [c for c in candidates if c.disposition == LLM_SUGGESTED_EXCLUDE]
    unexplained = [c for c in candidates if c.disposition == UNEXPLAINED_GAP]

    lines = [f"Exclude candidates for project={project} ip_version={ip_version}"]
    lines.append(
        f"  {len(suggested)} RTL-suggested exclude(s), {len(llm_suggested)} LLM-suggested exclude(s), "
        f"{len(unexplained)} unexplained gap(s)"
    )

    if suggested:
        lines.append(f"\nRTL-SUGGESTED EXCLUDES ({len(suggested)}) — deterministic match, still require human approval:")
        for c in suggested:
            lines.append(f"  {c.instance}.{c.signal}")
            lines.append(f"      {c.reason}")
            if c.source:
                lines.append(f"      source: {c.source}")

    if llm_suggested:
        lines.append(
            f"\nLLM-SUGGESTED EXCLUDES ({len(llm_suggested)}) — probabilistic, NOT a pattern match; "
            "requires approval AND a written note to include (see gen-excludes):"
        )
        for c in llm_suggested:
            lines.append(f"  {c.instance}.{c.signal}")
            lines.append(f"      {c.reason}")

    if unexplained:
        lines.append(f"\nUNEXPLAINED GAPS ({len(unexplained)}) — no RTL justification, needs a test:")
        for c in unexplained:
            lines.append(f"  {c.instance}.{c.signal}")
            lines.append(f"      {c.reason}")

    return "\n".join(lines)


def suggest_excludes(
    bins: list[ToggleBin],
    rtl: RtlFacts,
    params: dict,
    scope: str = "",
) -> list[ExcludeCandidate]:
    """Build exclude candidates for a set of toggle bins under one
    derivative's parameter values. `params` is the active config's own
    param overrides — do NOT pre-merge in RTL-declared defaults from
    every scanned file; each candidate is resolved against its own
    source file's declared defaults internally (see
    RtlFacts.params_for), which matters once you scan more than one
    RTL file that reuses a generic parameter name."""
    report = build_gap_report(bins, scope=scope)
    candidates: list[ExcludeCandidate] = []

    for b in report.never_toggled:
        candidates.append(_classify_never_toggled(b, rtl, params))

    for b in report.missing_0_to_1:
        candidates.append(
            ExcludeCandidate(
                instance=b.instance,
                signal=b.signal,
                disposition=UNEXPLAINED_GAP,
                reason="partial toggle gap (1->0 seen, 0->1 missing) — a tie-off/dead-branch kills both "
                "directions, so this can't be RTL-justified; needs a test to hit 0->1",
                source="",
                gap_kind="missing_0_to_1",
            )
        )
    for b in report.missing_1_to_0:
        candidates.append(
            ExcludeCandidate(
                instance=b.instance,
                signal=b.signal,
                disposition=UNEXPLAINED_GAP,
                reason="partial toggle gap (0->1 seen, 1->0 missing) — a tie-off/dead-branch kills both "
                "directions, so this can't be RTL-justified; needs a test to hit 1->0",
                source="",
                gap_kind="missing_1_to_0",
            )
        )

    _priority = {SUGGESTED_EXCLUDE: 0, LLM_SUGGESTED_EXCLUDE: 1, UNEXPLAINED_GAP: 2}
    candidates.sort(key=lambda c: (_priority.get(c.disposition, 9), c.instance, c.signal))
    return candidates
