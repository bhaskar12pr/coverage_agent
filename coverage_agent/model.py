"""Core data model for toggle coverage bins."""

import re
from dataclasses import dataclass, field

# Per-direction status words urg uses in real toggle detail tables.
COVERED = "covered"
NOT_COVERED = "not_covered"
UNREACHABLE = "unreachable"  # proven unreachable by VCS (-lca UNR analysis)
EXCLUDED = "excluded"        # excluded by a loaded .el file

_WAIVED = (UNREACHABLE, EXCLUDED)

_RANGE_RE = re.compile(r"\[(?P<hi>\d+)(?::(?P<lo>\d+))?\]$")


def bit_width(signal: str) -> int:
    """Width of a signal row as urg prints it: "x[56:6]" -> 51,
    "x[3]" -> 1, "x" -> 1. Only the trailing range counts; struct /
    array indices earlier in the name (alert_rx_i[0].ack_n) don't."""
    m = _RANGE_RE.search(signal)
    if not m:
        return 1
    hi = int(m.group("hi"))
    lo = int(m.group("lo")) if m.group("lo") is not None else hi
    return abs(hi - lo) + 1


@dataclass(frozen=True)
class ToggleBin:
    """One toggle row within one instance/module.

    Real urg reports collapse contiguous bits with identical status into
    one row ("data_i[56:6]"), so a bin can cover several bits — see
    `width`. The legacy synthetic samples are always one bit per row.
    """

    instance: str
    signal: str
    hit_0_to_1: bool
    hit_1_to_0: bool
    # --- optional detail, populated by the real-format (modinfo) parser ---
    status_0_to_1: str = ""        # COVERED / NOT_COVERED / UNREACHABLE / EXCLUDED
    status_1_to_0: str = ""
    direction: str = ""            # INPUT / OUTPUT / INOUT for ports, "" for signals
    tests_0_to_1: tuple[str, ...] = field(default=())
    tests_1_to_0: tuple[str, ...] = field(default=())
    annotation: str = ""           # urg "Exclude Annotation" column, e.g. "[UNR] Tie offs."
    module: str = ""               # module definition name, when known
    scope_kind: str = "instance"   # "instance" or "module" (module-definition section)

    @property
    def full_name(self) -> str:
        return f"{self.instance}.{self.signal}" if self.instance else self.signal

    @property
    def width(self) -> int:
        return bit_width(self.signal)

    @property
    def excluded(self) -> bool:
        return EXCLUDED in (self.status_0_to_1, self.status_1_to_0)

    @property
    def unreachable(self) -> bool:
        return UNREACHABLE in (self.status_0_to_1, self.status_1_to_0)

    @property
    def waived(self) -> bool:
        """Both directions accounted for by the tool flow (excluded by an
        .el file or proven unreachable) — nothing left to chase."""
        return self.status_0_to_1 in _WAIVED and self.status_1_to_0 in _WAIVED

    @property
    def partly_waived(self) -> bool:
        return (self.status_0_to_1 in _WAIVED) != (self.status_1_to_0 in _WAIVED)

    def effective(self) -> "ToggleBin":
        """This row with any waived direction treated as hit, so gap
        logic only judges the directions that still count."""
        if not self.partly_waived:
            return self
        from dataclasses import replace

        return replace(
            self,
            hit_0_to_1=self.hit_0_to_1 or self.status_0_to_1 in _WAIVED,
            hit_1_to_0=self.hit_1_to_0 or self.status_1_to_0 in _WAIVED,
        )

    @property
    def fully_covered(self) -> bool:
        return self.hit_0_to_1 and self.hit_1_to_0

    @property
    def never_toggled(self) -> bool:
        return not self.hit_0_to_1 and not self.hit_1_to_0

    @property
    def is_gap(self) -> bool:
        """At least one direction that isn't waived was never hit."""
        return any(
            status not in _WAIVED and not hit
            for status, hit in ((self.status_0_to_1, self.hit_0_to_1), (self.status_1_to_0, self.hit_1_to_0))
        )

    @property
    def missing_directions(self) -> list[str]:
        missing = []
        if not self.hit_0_to_1:
            missing.append("0->1")
        if not self.hit_1_to_0:
            missing.append("1->0")
        return missing
