"""Fictional payers and practices. Any resemblance to a real plan is accidental."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Payer:
    payer_id: str
    name: str
    prefix: str
    sends_allowed: bool
    """Whether this payer's 835s carry AMT*B6, so the auditor must cope with both."""


@dataclass(frozen=True)
class Practice:
    name: str
    npi: str


PAYERS = (
    Payer("60054", "Northwind Health Plan", "NW", sends_allowed=True),
    Payer("71320", "Harborview Mutual", "HM", sends_allowed=False),
    Payer("38811", "Keystone Benefit Co", "KB", sends_allowed=True),
    Payer("52007", "Lakeside Care Alliance", "LC", sends_allowed=False),
)

PRACTICE_NAMES = (
    "Hudson Orthopedic Associates",
    "Riverside Family Medicine",
    "Bowery Pain and Spine",
    "Astoria Sports Medicine",
    "Flatiron Dermatology Group",
    "Inwood Primary Care",
)


def npi_with_check_digit(base9: str) -> str:
    """NPI check digit: Luhn over the 80840 prefix plus the first nine digits."""
    digits = [int(c) for c in "80840" + base9]
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return base9 + str((10 - total % 10) % 10)


def make_practice(rng: random.Random) -> Practice:
    base = "1" + "".join(str(rng.randrange(10)) for _ in range(8))
    return Practice(name=rng.choice(PRACTICE_NAMES), npi=npi_with_check_digit(base))
