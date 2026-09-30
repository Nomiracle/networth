"""Minimal stdlib-only XLSX reader.

No openpyxl: reads the OOXML package directly (shared strings, sheet grids,
merged-cell fill). Enough for the networth M0 extraction pass.

Public API:
    wb = read_workbook(path)
    wb.sheet_names          -> ["总览", "2025-8-18", ...]
    wb.grid(name)           -> {(row, col): value}  1-based, merged cells filled
    wb.max_row(name), wb.max_col(name)
"""
from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_R_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def col_to_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def parse_ref(ref: str):
    m = _CELL_REF.match(ref)
    if not m:
        return None
    return int(m.group(2)), col_to_idx(m.group(1))


class Workbook:
    def __init__(self, path: str):
        self.path = path
        self._z = zipfile.ZipFile(path)
        self._shared = self._read_shared()
        self._sheets = self._read_sheet_index()
        self._grids: dict[str, dict] = {}
        self._dims: dict[str, tuple[int, int]] = {}

    # ---------- package plumbing ----------
    def _read_shared(self) -> list[str]:
        try:
            raw = self._z.read("xl/sharedStrings.xml")
        except KeyError:
            return []
        root = ET.fromstring(raw)
        out: list[str] = []
        for si in root.findall(f"{NS}si"):
            parts = [t.text or "" for t in si.iter(f"{NS}t")]
            out.append("".join(parts))
        return out

    def _read_sheet_index(self) -> list[tuple[str, str]]:
        wb = ET.fromstring(self._z.read("xl/workbook.xml"))
        rels = ET.fromstring(self._z.read("xl/_rels/workbook.xml.rels"))
        rel_target = {
            r.get("Id"): r.get("Target")
            for r in rels.findall(f"{PKG_R_NS}Relationship")
        }
        sheets: list[tuple[str, str]] = []
        for sh in wb.find(f"{NS}sheets").findall(f"{NS}sheet"):
            name = sh.get("name")
            rid = sh.get(f"{R_NS}id")
            target = rel_target.get(rid, "")
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            sheets.append((name, target))
        return sheets

    # ---------- public ----------
    @property
    def sheet_names(self) -> list[str]:
        return [n for n, _ in self._sheets]

    def grid(self, name: str) -> dict:
        if name not in self._grids:
            target = dict(self._sheets)[name]
            self._grids[name], self._dims[name] = self._parse_sheet(target)
        return self._grids[name]

    def max_row(self, name: str) -> int:
        self.grid(name)
        return self._dims[name][0]

    def max_col(self, name: str) -> int:
        self.grid(name)
        return self._dims[name][1]

    # ---------- sheet parsing ----------
    def _cell_value(self, c, row_num: int, col_num: int):
        t = c.get("t")
        if t == "inlineStr":
            return "".join(x.text or "" for x in c.iter(f"{NS}t"))
        v = c.find(f"{NS}v")
        if v is None or v.text is None:
            return None
        raw = v.text
        if t == "s":
            try:
                return self._shared[int(raw)]
            except (ValueError, IndexError):
                return None
        if t == "str":
            return raw
        if t == "b":
            return raw == "1"
        try:
            f = float(raw)
        except ValueError:
            return raw
        return f

    def _parse_sheet(self, target: str):
        root = ET.fromstring(self._z.read(target))
        grid: dict = {}
        max_r = max_c = 0
        data = root.find(f"{NS}sheetData")
        if data is None:
            return grid, (0, 0)
        for row in data.findall(f"{NS}row"):
            r = int(row.get("r") or 0)
            for c in row.findall(f"{NS}c"):
                ref = c.get("r")
                if not ref:
                    continue
                rc = parse_ref(ref)
                if not rc:
                    continue
                rr, cc = rc
                val = self._cell_value(c, rr, cc)
                if val is None or (isinstance(val, str) and val.strip() == ""):
                    continue
                grid[(rr, cc)] = val
                max_r, max_c = max(max_r, rr), max(max_c, cc)
        # fill merged cells so vertical merges can be forward-filled naturally
        merges = root.find(f"{NS}mergeCells")
        if merges is not None:
            for mc in merges.findall(f"{NS}mergeCell"):
                ref = mc.get("ref") or ""
                if ":" not in ref:
                    continue
                a, b = ref.split(":", 1)
                pa, pb = parse_ref(a), parse_ref(b)
                if not pa or not pb:
                    continue
                anchor = grid.get(pa)
                if anchor is None:
                    continue
                for rr in range(pa[0], pb[0] + 1):
                    for cc in range(pa[1], pb[1] + 1):
                        grid.setdefault((rr, cc), anchor)
        return grid, (max_r, max_c)


def read_workbook(path: str) -> Workbook:
    return Workbook(path)
