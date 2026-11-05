"""Medicare fee lookups.

The bundled table is synthetic: real codes, invented amounts, the same shape as the CMS
Physician Fee Schedule (a fee per code per year plus a multiple-procedure indicator).
Swap in the real PFS file by loading it through `FeeTable.from_csv`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class FeeRow:
    code: str
    category: str
    mpr_indicator: int
    """CMS convention: 2 means the standard multiple-procedure reduction applies."""
    fees: dict[int, Decimal]


class FeeTable:
    def __init__(self, rows: dict[str, FeeRow]):
        self._rows = rows

    @classmethod
    def from_csv(cls, path: Path | str) -> FeeTable:
        with open(path, newline="") as handle:
            return cls._parse(handle.read())

    @classmethod
    def _parse(cls, text: str) -> FeeTable:
        rows: dict[str, FeeRow] = {}
        reader = csv.DictReader(text.splitlines())
        for record in reader:
            fees = {
                int(key.removeprefix("fee_")): Decimal(value)
                for key, value in record.items()
                if key.startswith("fee_") and value
            }
            rows[record["code"]] = FeeRow(
                code=record["code"],
                category=record["category"],
                mpr_indicator=int(record["mpr_indicator"]),
                fees=fees,
            )
        return cls(rows)

    def fee(self, code: str, year: int) -> Decimal | None:
        row = self._rows.get(code)
        return None if row is None else row.fees.get(year)

    def mpr_eligible(self, code: str) -> bool:
        row = self._rows.get(code)
        return row is not None and row.mpr_indicator == 2

    def row(self, code: str) -> FeeRow | None:
        return self._rows.get(code)

    def codes(self) -> list[str]:
        return sorted(self._rows)


@cache
def default_fee_table() -> FeeTable:
    resource = files("recoup.core.data").joinpath("medicare_fees_synthetic.csv")
    return FeeTable._parse(resource.read_text())
