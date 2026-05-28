"""Maps LLM-produced ``scam_category`` enum values to the FTC form's
``ddlSubjectMatter`` dropdown values (0..16).

The dropdown values match donotcall.gov as of this writing:

    0  -- I don't know
    1  -- Other
    2  -- Dropped call or no message
    3  -- Reducing your debt
    4  -- Calls pretending to be government, businesses, or family / friends
    5  -- Medical & prescriptions
    6  -- Home improvement & cleaning
    7  -- Computer & technical support
    8  -- Energy, solar, and utilities
    9  -- Home security & alarms
    10 -- Travel & timeshares
    11 -- Investing & retirement
    12 -- Warranties & protection plans
    13 -- Lottery, prizes & sweepstakes
    14 -- Vacation & timeshare
    15 -- Political
    16 -- Charity / donations

If a category isn't recognised, we fall back to ``1`` ("Other") and pass
the raw category through as the free-text ``txtSubjectMatter`` value.
"""

from __future__ import annotations

from typing import Optional


SCAM_CATEGORIES: list[str] = [
    "tax_relief",
    "government_impersonation",
    "irs_scam",
    "social_security_scam",
    "debt_consolidation",
    "loan_offer",
    "student_loan",
    "credit_card",
    "auto_warranty",
    "home_warranty",
    "medical",
    "medicare",
    "health_insurance",
    "home_security",
    "home_improvement",
    "solar",
    "utilities",
    "energy",
    "tech_support",
    "lottery",
    "sweepstakes",
    "prize",
    "vacation",
    "timeshare",
    "travel",
    "investment",
    "retirement",
    "crypto",
    "charity",
    "political",
    "dropped_call",
    "unknown",
    "other",
]


_CATEGORY_TO_SUBJECT_ID: dict[str, int] = {
    # Government / impersonation
    "tax_relief": 4,
    "government_impersonation": 4,
    "irs_scam": 4,
    "social_security_scam": 4,

    # Debt
    "debt_consolidation": 3,
    "loan_offer": 3,
    "student_loan": 3,
    "credit_card": 3,

    # Warranties
    "auto_warranty": 12,
    "home_warranty": 12,

    # Medical
    "medical": 5,
    "medicare": 5,
    "health_insurance": 5,

    # Home
    "home_security": 9,
    "home_improvement": 6,

    # Energy / utilities
    "solar": 8,
    "utilities": 8,
    "energy": 8,

    # Tech
    "tech_support": 7,

    # Prizes / lottery
    "lottery": 13,
    "sweepstakes": 13,
    "prize": 13,

    # Vacation
    "vacation": 14,
    "timeshare": 14,
    "travel": 10,

    # Investment
    "investment": 11,
    "retirement": 11,
    "crypto": 11,

    # Misc — never map to 15 (Political): FTC shows a dead-end page and
    # does not accept the complaint. Use "Other" instead.
    "charity": 16,
    "political": 1,
    "dropped_call": 2,
    "unknown": 0,
    "other": 1,
}


SUBJECT_ID_LABELS: dict[int, str] = {
    0: "I don't know",
    1: "Other",
    2: "Dropped call or no message",
    3: "Reducing your debt",
    4: "Calls pretending to be government, businesses, or family / friends",
    5: "Medical & prescriptions",
    6: "Home improvement & cleaning",
    7: "Computer & technical support",
    8: "Energy, solar, and utilities",
    9: "Home security & alarms",
    10: "Travel & timeshares",
    11: "Investing & retirement",
    12: "Warranties & protection plans",
    13: "Lottery, prizes & sweepstakes",
    14: "Vacation & timeshare",
    15: "Political",
    16: "Charity / donations",
}


def map_scam_category(
    scam_category: Optional[str],
) -> tuple[int, Optional[str]]:
    """Return ``(ddlSubjectMatter, free_text)`` for a given scam_category.

    ``free_text`` is non-None only when we fall back to "Other"; the
    caller should write it into ``#txtSubjectMatter`` on the FTC form.
    """
    if not scam_category:
        return 0, None
    key = scam_category.strip().lower()
    if key == "political":
        return 1, "Unwanted telemarketing robocall"
    if key in _CATEGORY_TO_SUBJECT_ID:
        subject_id = _CATEGORY_TO_SUBJECT_ID[key]
        return subject_id, None
    return 1, scam_category.strip()
