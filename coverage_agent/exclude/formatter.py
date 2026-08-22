"""Render human-approved exclude candidates as a VCS-style coverage
exclusion file.

FORMAT UNVERIFIED: built from documented VCS/urg UCM exclusion
conventions (INSTANCE + Exclude Toggle blocks with a -comment
justification), not validated against a real exclusion file from your
VCS install. Confirm the syntax matches before loading this into a
real coverage run — see README.md.
"""

from collections import defaultdict

from coverage_agent.exclude.candidates import LLM_SUGGESTED_EXCLUDE, SUGGESTED_EXCLUDE, UNEXPLAINED_GAP

# Dispositions that are NOT a deterministic RTL pattern match — a
# human must write a "note" to include these, same bar as a manual
# override on a plain unexplained_gap. LLM opinions are probabilistic,
# not proof, so they don't get the lighter bar that SUGGESTED_EXCLUDE gets.
_REQUIRES_NOTE = (UNEXPLAINED_GAP, LLM_SUGGESTED_EXCLUDE)


def format_ucm_exclusions(review_entries: list[dict]) -> tuple[str, list[str]]:
    """Returns (exclusion_file_text, warnings). Only entries with
    approved == true are emitted. An approved entry whose disposition
    isn't a deterministic RTL match (a plain "unexplained_gap" manual
    override, or an "llm_suggested_exclude" LLM opinion) is only
    emitted if it carries a non-empty "note" explaining why; otherwise
    it's skipped and reported as a warning, never silently excluded."""
    warnings: list[str] = []
    by_instance: dict[str, list[dict]] = defaultdict(list)

    for entry in review_entries:
        if entry.get("approved") is not True:
            continue
        disposition = entry.get("disposition")
        if disposition in _REQUIRES_NOTE and not (entry.get("note") or "").strip():
            warnings.append(
                f"skipped {entry.get('id')}: approved but disposition is '{disposition}' "
                "with no override 'note' explaining why — add a note to include it"
            )
            continue
        if disposition not in (SUGGESTED_EXCLUDE, LLM_SUGGESTED_EXCLUDE, UNEXPLAINED_GAP):
            warnings.append(f"skipped {entry.get('id')}: unknown disposition {disposition!r}")
            continue
        by_instance[entry["instance"]].append(entry)

    lines = [
        "// coverage_agent generated exclusion file",
        "// FORMAT UNVERIFIED against a real VCS run -- validate before use (see README.md)",
        f"// {sum(len(v) for v in by_instance.values())} exclusion(s) from human-approved candidates",
        "",
    ]

    for instance in sorted(by_instance):
        lines.append(f"INSTANCE: {instance}")
        for entry in sorted(by_instance[instance], key=lambda e: e["signal"]):
            reason = entry.get("reason", "")
            disp = entry.get("disposition")
            if disp == UNEXPLAINED_GAP:
                reason = f"MANUAL OVERRIDE: {entry.get('note')} (tool reason: {reason})"
            elif disp == LLM_SUGGESTED_EXCLUDE:
                reason = f"LLM SUGGESTION, HUMAN-CONFIRMED: {entry.get('note')} (LLM reason: {reason})"
            source = entry.get("source") or ""
            comment = f"{reason} ({source})" if source else reason
            comment = comment.replace('"', "'")
            lines.append(f'Exclude Toggle "{entry["signal"]}" -detail "01" -comment "{comment}"')
        lines.append("")

    return "\n".join(lines), warnings
