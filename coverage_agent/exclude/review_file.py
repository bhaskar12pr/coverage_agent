"""Human-review file for exclude candidates. This is the required
sign-off step: suggest_excludes() only proposes; a human must open
this JSON file, set "approved": true on entries they actually agree
with, and only THEN can format_ucm_exclusions() emit anything."""

import json

from coverage_agent.exclude.candidates import ExcludeCandidate


def _to_dict(c: ExcludeCandidate, project: str, ip_version: str) -> dict:
    return {
        "id": c.candidate_id,
        "instance": c.instance,
        "signal": c.signal,
        "disposition": c.disposition,
        "reason": c.reason,
        "source": c.source,
        "project": project,
        "ip_version": ip_version,
        "approved": c.approved,
        "reviewer": c.reviewer,
        "note": c.note,
    }


def write_review_file(path: str, candidates: list[ExcludeCandidate], project: str, ip_version: str) -> None:
    payload = [_to_dict(c, project, ip_version) for c in candidates]
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def load_review_file(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
