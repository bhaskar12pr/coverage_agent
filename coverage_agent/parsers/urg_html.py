"""Parser for VCS urg toggle coverage reports in -format html.

Uses stdlib html.parser (no external deps, so this runs in locked-down
DV environments without pip access). Finds toggle tables by header
keywords ("Toggle 0->1" / "Toggle 1->0" / "Name") rather than fixed
class names or ids, since those vary across VCS versions.

Validate against a real urgReport/*.html file and adjust the keyword
matching below if it doesn't line up (see README.md).
"""

import html
import re
from html.parser import HTMLParser

from coverage_agent.model import ToggleBin

_INSTANCE_RE = re.compile(
    r"(?:toggle coverage\s+for\s+instance|instance)\s*[:\-]\s*(?P<inst>\S+)",
    re.IGNORECASE,
)

_ARROW_RE = re.compile(r"-?>|-&gt;|→|\bto\b", re.IGNORECASE)


def _normalize(text: str) -> str:
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _col_kind(header_text: str) -> str | None:
    t = _normalize(header_text).lower()
    if "0" in t and "1" in t and _ARROW_RE.search(t) and t.index("0") < t.index("1"):
        return "0_to_1"
    if "1" in t and "0" in t and _ARROW_RE.search(t) and t.index("1") < t.index("0"):
        return "1_to_0"
    if t in ("name", "signal", "signal name", "node"):
        return "name"
    return None


def _status_to_bool(token: str) -> bool:
    t = token.strip().lower()
    if t in ("covered", "yes"):
        return True
    if t in ("not covered", "no", ""):
        return False
    try:
        return int(t) > 0
    except ValueError:
        return False


class _UrgHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bins: list[ToggleBin] = []
        self.current_instance = ""

        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._row_cells: list[str] = []
        self._table_header: list[str] = []
        self._col_kinds: list[str | None] = []
        self._pending_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
            self._table_header = []
            self._col_kinds = []
        elif tag == "tr" and self._in_table:
            self._in_row = True
            self._row_cells = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_text = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._row_cells.append(_normalize("".join(self._cell_text)))
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if not self._col_kinds:
                kinds = [_col_kind(c) for c in self._row_cells]
                if "0_to_1" in kinds and "1_to_0" in kinds:
                    self._col_kinds = kinds
                    self._table_header = self._row_cells
            else:
                self._consume_data_row(self._row_cells)
        elif tag == "table":
            self._in_table = False
            self._col_kinds = []
            self._table_header = []

    def handle_data(self, data):
        if self._in_cell:
            self._cell_text.append(data)
        elif not self._in_table:
            m = _INSTANCE_RE.search(data)
            if m:
                self.current_instance = m.group("inst").rstrip(":")

    def _consume_data_row(self, cells: list[str]) -> None:
        name = None
        c01 = None
        c10 = None
        name_idx = None
        for idx, kind in enumerate(self._col_kinds):
            if kind == "name":
                name_idx = idx
        for idx, cell in enumerate(cells):
            if idx >= len(self._col_kinds):
                continue
            kind = self._col_kinds[idx]
            if kind == "0_to_1":
                c01 = cell
            elif kind == "1_to_0":
                c10 = cell
        if name_idx is not None and name_idx < len(cells):
            name = cells[name_idx]
        elif cells:
            name = cells[0]

        if name and c01 is not None and c10 is not None:
            self.bins.append(
                ToggleBin(
                    instance=self.current_instance,
                    signal=name,
                    hit_0_to_1=_status_to_bool(c01),
                    hit_1_to_0=_status_to_bool(c10),
                )
            )


def parse_urg_html(text: str) -> list[ToggleBin]:
    parser = _UrgHtmlParser()
    parser.feed(text)
    return parser.bins


def parse_urg_html_file(path: str) -> list[ToggleBin]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_urg_html(f.read())
