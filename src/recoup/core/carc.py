"""The claim adjustment reason codes (CARC) and remark codes (RARC) Recoup reasons about.

Descriptions are short paraphrases, not the official X12 text.
"""

CARC = {
    "1": "deductible",
    "2": "coinsurance",
    "3": "copay",
    "4": "procedure code inconsistent with modifier",
    "16": "claim lacks information needed for adjudication",
    "18": "duplicate claim or service",
    "29": "time limit for filing has expired",
    "45": "charge exceeds fee schedule or contracted amount",
    "59": "processed under multiple or concurrent procedure rules",
    "96": "non-covered charge",
    "97": "payment included in another service",
    "197": "precertification or authorization absent",
    "253": "sequestration",
}

RARC = {
    "M80": "not covered when performed with another service on the same day",
    "N130": "consult plan benefit documents",
    "N211": "alert: you may not appeal this decision",
    "MA130": "claim contains incomplete or invalid information",
}

DENIAL_REASONS = {"16", "18", "29", "96", "97", "197"}
"""CARCs that zero out a line rather than reprice it."""

PATIENT_REASONS = {"1", "2", "3"}


def describe_carc(code: str) -> str:
    return CARC.get(code, "unrecognized adjustment reason")
