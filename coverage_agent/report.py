"""Toggle coverage gap analysis and report formatting."""

import csv
import io
import json
from dataclasses import dataclass

from coverage_agent.model import ToggleBin


@dataclass
class GapReport:
    scope: str
    total: int
    fully_covered: int
    never_toggled: list[ToggleBin]
    missing_0_to_1: list[ToggleBin]
    missing_1_to_0: list[ToggleBin]

    @property
    def coverage_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return 100.0 * self.fully_covered / self.total

    @property
    def gap_count(self) -> int:
        return len(self.never_toggled) + len(self.missing_0_to_1) + len(self.missing_1_to_0)


def build_gap_report(bins: list[ToggleBin], scope: str = "") -> GapReport:
    """Group toggle bins into coverage gap categories, optionally scoped
    to a VIP/instance hierarchy prefix (e.g. "tb_top.dut.u_axi_vip")."""
    if scope:
        bins = [b for b in bins if b.full_name.startswith(scope)]

    never_toggled = [b for b in bins if b.never_toggled]
    missing_0_to_1 = [b for b in bins if not b.never_toggled and not b.hit_0_to_1]
    missing_1_to_0 = [b for b in bins if not b.never_toggled and not b.hit_1_to_0]
    fully_covered = sum(1 for b in bins if b.fully_covered)

    never_toggled.sort(key=lambda b: b.full_name)
    missing_0_to_1.sort(key=lambda b: b.full_name)
    missing_1_to_0.sort(key=lambda b: b.full_name)

    return GapReport(
        scope=scope,
        total=len(bins),
        fully_covered=fully_covered,
        never_toggled=never_toggled,
        missing_0_to_1=missing_0_to_1,
        missing_1_to_0=missing_1_to_0,
    )


def format_console(report: GapReport, top: int | None = None) -> str:
    lines = []
    scope_desc = report.scope or "(all instances)"
    lines.append(f"Toggle coverage gap report — scope: {scope_desc}")
    lines.append(
        f"  {report.fully_covered}/{report.total} bins fully covered "
        f"({report.coverage_pct:.2f}%), {report.gap_count} gap(s)"
    )

    def _section(title: str, items: list[ToggleBin]) -> None:
        if not items:
            return
        shown = items if top is None else items[:top]
        lines.append(f"\n{title} ({len(items)}):")
        for b in shown:
            lines.append(f"  {b.full_name}")
        if top is not None and len(items) > top:
            lines.append(f"  ... and {len(items) - top} more")

    _section("NEVER TOGGLED (never seen 0->1 or 1->0)", report.never_toggled)
    _section("MISSING 0->1 transition", report.missing_0_to_1)
    _section("MISSING 1->0 transition", report.missing_1_to_0)

    if report.gap_count == 0 and report.total > 0:
        lines.append("\nNo gaps — toggle coverage is 100% in this scope.")
    elif report.total == 0:
        lines.append("\nNo toggle bins found in this scope. Check --scope or input path.")

    return "\n".join(lines)


def to_json(report: GapReport) -> str:
    def _bins(items: list[ToggleBin]) -> list[dict]:
        return [
            {
                "instance": b.instance,
                "signal": b.signal,
                "hit_0_to_1": b.hit_0_to_1,
                "hit_1_to_0": b.hit_1_to_0,
            }
            for b in items
        ]

    payload = {
        "scope": report.scope,
        "total": report.total,
        "fully_covered": report.fully_covered,
        "coverage_pct": round(report.coverage_pct, 2),
        "gap_count": report.gap_count,
        "never_toggled": _bins(report.never_toggled),
        "missing_0_to_1": _bins(report.missing_0_to_1),
        "missing_1_to_0": _bins(report.missing_1_to_0),
    }
    return json.dumps(payload, indent=2)


def to_csv(report: GapReport) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["category", "instance", "signal", "hit_0_to_1", "hit_1_to_0"])
    for category, items in (
        ("never_toggled", report.never_toggled),
        ("missing_0_to_1", report.missing_0_to_1),
        ("missing_1_to_0", report.missing_1_to_0),
    ):
        for b in items:
            writer.writerow([category, b.instance, b.signal, b.hit_0_to_1, b.hit_1_to_0])
    return buf.getvalue()
