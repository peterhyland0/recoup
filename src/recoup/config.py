"""Tenant configuration. Onboarding a new practice is one TOML file: its payers and its policy."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from recoup.ingest.spans import skeleton


class PayerConfig(BaseModel):
    payer_id: str
    names: list[str]
    """How this payer is written in contracts and remits. Matched OCR-tolerantly."""


class Policy(BaseModel):
    confidence_threshold: float = 0.85
    """Findings that rest on a term below this confidence go to a human."""
    tolerance: Decimal = Decimal("0.01")
    """Variances smaller than this are rounding, not underpayment."""
    min_appeal_amount: Decimal = Decimal("5.00")
    """Below this, an appeal costs more to file than it recovers."""


class Tenant(BaseModel):
    tenant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    """Lowercase letters, digits, '-' and '_'. Never '*', which the database reserves for system work."""
    name: str
    payers: list[PayerConfig]
    policy: Policy = Field(default_factory=Policy)

    def payer_for(self, name: str) -> PayerConfig | None:
        wanted = skeleton(name)
        for payer in self.payers:
            if any(skeleton(alias) in wanted or wanted in skeleton(alias) for alias in payer.names):
                return payer
        return None


def load_tenant(path: Path | str) -> Tenant:
    with open(path, "rb") as handle:
        return Tenant.model_validate(tomllib.load(handle))


DEMO_TENANT = Tenant(
    tenant_id="demo",
    name="Demo practice group",
    payers=[
        PayerConfig(payer_id="60054", names=["Northwind Health Plan"]),
        PayerConfig(payer_id="71320", names=["Harborview Mutual"]),
        PayerConfig(payer_id="38811", names=["Keystone Benefit Co"]),
        PayerConfig(payer_id="52007", names=["Lakeside Care Alliance"]),
    ],
)
