"""Parser for VCS urg toggle coverage reports in -format text.

The exact column layout of urg's text output has drifted across VCS
versions, so this parser matches on keywords/patterns rather than fixed
column positions:

  - status words: "Covered" / "Not Covered" / "Yes" / "No"
  - raw hit counts: two integers (0 == not covered, >0 == covered)

Validate against a real report from your VCS version and adjust the
regexes below if signal names or statuses don't match (see README.md).
"""

import re

from coverage_agent.model import ToggleBin

_INSTANCE_RE = re.compile(
    r"(?:toggle coverage\s+for\s+instance|instance)\s*[:\-]\s*(?P<inst>\S+)",
    re.IGNORECASE,
)

_STATUS = r"(?:Covered|Not\s+Covered|Yes|No)"
_ROW_STATUS_RE = re.compile(
    r"^\s*(?:\d+\s+)?"                          # optional leading line number
    r"(?P<name>[\w.\[\]:/$]+)\s+"               # hierarchical/bit-indexed signal name
    r"(?P<c1>" + _STATUS + r")\s+"
    r"(?P<c2>" + _STATUS + r")\s*$",
    re.IGNORECASE,
)

_ROW_COUNT_RE = re.compile(
    r"^\s*(?:\d+\s+)?"
    r"(?P<name>[\w.\[\]:/$]+)\s+"
    r"(?P<c1>\d+)\s+"
    r"(?P<c2>\d+)\s*(?:\d+(?:\.\d+)?%?\s*)?(?:\d+(?:\.\d+)?%?\s*)?$",
)

_SKIP_HEADER_WORDS = ("name", "toggle", "line", "====", "----")


def _is_header_or_separator(line: str) -> bool:
    stripped = line.strip().lower()
    if not stripped:
        return True
    if set(stripped) <= {"=", "-", " "}:
        return True
    return any(stripped.startswith(w) for w in _SKIP_HEADER_WORDS)


def _status_to_bool(token: str) -> bool:
    return token.strip().lower() in ("covered", "yes")


def parse_urg_text(text: str) -> list[ToggleBin]:
    """Parse toggle bins out of a urg -format text report body."""
    bins: list[ToggleBin] = []
    current_instance = ""

    for line in text.splitlines():
        inst_match = _INSTANCE_RE.search(line)
        if inst_match:
            current_instance = inst_match.group("inst").rstrip(":")
            continue

        if _is_header_or_separator(line):
            continue

        m = _ROW_STATUS_RE.match(line)
        if m:
            bins.append(
                ToggleBin(
                    instance=current_instance,
                    signal=m.group("name"),
                    hit_0_to_1=_status_to_bool(m.group("c1")),
                    hit_1_to_0=_status_to_bool(m.group("c2")),
                )
            )
            continue

        m = _ROW_COUNT_RE.match(line)
        if m:
            bins.append(
                ToggleBin(
                    instance=current_instance,
                    signal=m.group("name"),
                    hit_0_to_1=int(m.group("c1")) > 0,
                    hit_1_to_0=int(m.group("c2")) > 0,
                )
            )

    return bins


def parse_urg_text_file(path: str) -> list[ToggleBin]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_urg_text(f.read())
