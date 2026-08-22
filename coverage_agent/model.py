"""Core data model for toggle coverage bins."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToggleBin:
    """One signal's toggle coverage status within one instance/module."""

    instance: str
    signal: str
    hit_0_to_1: bool
    hit_1_to_0: bool

    @property
    def full_name(self) -> str:
        return f"{self.instance}.{self.signal}" if self.instance else self.signal

    @property
    def fully_covered(self) -> bool:
        return self.hit_0_to_1 and self.hit_1_to_0

    @property
    def never_toggled(self) -> bool:
        return not self.hit_0_to_1 and not self.hit_1_to_0

    @property
    def missing_directions(self) -> list[str]:
        missing = []
        if not self.hit_0_to_1:
            missing.append("0->1")
        if not self.hit_1_to_0:
            missing.append("1->0")
        return missing
