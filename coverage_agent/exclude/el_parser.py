"""Parser for the UCM-style exclusion file format coverage_agent
generates in formatter.py (INSTANCE: + Exclude Toggle "sig" blocks).
Exists so verify.py can audit a real generated .el file, not just the
review JSON — e.g. one an owner hand-edited after generation, or one
that predates coverage_agent entirely but follows the same shape.

Same caveat as formatter.py: this matches OUR OWN generated syntax,
which is itself unverified against a real VCS exclusion file — see
README.md.
"""

import re

_INSTANCE_RE = re.compile(r"^INSTANCE:\s*(?P<inst>\S+)")
_EXCLUDE_RE = re.compile(r'^Exclude\s+Toggle\s+"(?P<sig>[^"]+)"')


def parse_ucm_exclusion_file(text: str) -> list[tuple[str, str]]:
    """Returns [(instance, signal), ...] in file order."""
    pairs: list[tuple[str, str]] = []
    current_instance = ""
    for line in text.splitlines():
        m = _INSTANCE_RE.match(line.strip())
        if m:
            current_instance = m.group("inst")
            continue
        m = _EXCLUDE_RE.match(line.strip())
        if m:
            pairs.append((current_instance, m.group("sig")))
    return pairs
