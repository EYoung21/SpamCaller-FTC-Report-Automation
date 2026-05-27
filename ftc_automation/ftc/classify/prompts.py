"""Prompt + JSON schema for the OpenAI spam classifier."""

from __future__ import annotations

from .ftc_mapping import SCAM_CATEGORIES


SYSTEM_PROMPT = """\
You analyze voicemail transcripts left on a personal phone line and decide
whether each one is a spam / robocall that should be reported to the FTC's
National Do Not Call Registry at donotcall.gov.

Return STRICTLY valid JSON that matches the provided schema. Never include
prose outside the JSON. Be conservative: if a transcript is ambiguous
(e.g. a real-sounding short personal message) set is_spam=false.

Guidance:
- Common spam pitches: tax relief / "IRS penalty abatement", debt
  consolidation, student loan forgiveness, auto/home warranty, Medicare or
  health insurance, solar, home security, tech support, lottery /
  sweepstakes, free cruise / vacation, charity / political that pretends
  to be from a known org. Pre-recorded ("robocall") messages are almost
  always spam.
- callback_number must be extracted from the transcript if the caller
  reads one out (e.g. "call us back at 833-893-2138"). Normalise to
  +1XXXXXXXXXX. If absent, leave it null.
- claimed_company is whatever name / brand the caller uses for themselves.
  If the caller is unintelligible or uses a non-word ("teal"), set it to
  the closest thing and add an editorial note in parentheses.
- scam_category MUST be one of the enum values in the schema.
- ftc_comment is the prose that will be pasted into the FTC complaint
  form's CommentTextBox. Keep it under 900 characters. Mention: caller
  number, callback number, claimed identity, the pitch, and a short
  quoted excerpt from the transcript. Write it in first person ("They
  called and said...") since the user is the one filing.
- should_report should be true for any clear spam robocall (regardless
  of whether they left a callback number). It should be false for clear
  non-spam (a person leaving a personal message, an appointment
  reminder, etc.).
"""


FEW_SHOT_EXAMPLES = [
    {
        "transcript": (
            "Hi, this message is for the responsible party. This is Bennett "
            "from the debt resolution office. We've reviewed your file and you "
            "may qualify for a hardship reduction of up to 70% on your "
            "unsecured debts. Please give us a call back at 833-893-2138, "
            "reference number 4421. Thank you."
        ),
        "caller_number": "+15555550100",
        "expected": {
            "is_spam": True,
            "confidence": 0.97,
            "callback_number": "+18338932138",
            "claimed_company": "the debt resolution office (\"Bennett\")",
            "scam_category": "debt_consolidation",
            "summary": "Robocall offering up to 70% debt reduction; gave callback 833-893-2138.",
            "ftc_comment": (
                "They called from +15555550100 and left a callback number of "
                "+18338932138. The caller identified themselves as \"Bennett "
                "from the debt resolution office\" and said I may qualify for "
                "a hardship reduction of up to 70% on unsecured debts, "
                "reference number 4421. Transcript: 'Hi, this message is for "
                "the responsible party...'"
            ),
            "should_report": True,
        },
    },
    {
        "transcript": (
            "Hey, just calling, just calling to see if you're free for dinner "
            "Sunday. Call me back when you get a chance, love you."
        ),
        "caller_number": "+18585551212",
        "expected": {
            "is_spam": False,
            "confidence": 0.99,
            "callback_number": None,
            "claimed_company": None,
            "scam_category": "unknown",
            "summary": "Personal message from a someone known to the recipient.",
            "ftc_comment": "",
            "should_report": False,
        },
    },
    {
        "transcript": (
            "This is the second notice regarding your vehicle's extended "
            "warranty. Our records indicate your coverage has expired. To "
            "speak with a representative press one or call 877-555-0143."
        ),
        "caller_number": "+18005550199",
        "expected": {
            "is_spam": True,
            "confidence": 0.99,
            "callback_number": "+18775550143",
            "claimed_company": "auto warranty department (unspecified)",
            "scam_category": "auto_warranty",
            "summary": "Classic auto-warranty robocall, gave callback 877-555-0143.",
            "ftc_comment": (
                "They called from +18005550199 and left a callback number of "
                "+18775550143. Pre-recorded message claimed to be a 'second "
                "notice' about my vehicle's extended warranty expiring and "
                "instructed me to press 1 or call back. Transcript: 'This is "
                "the second notice regarding your vehicle's extended "
                "warranty...'"
            ),
            "should_report": True,
        },
    },
]


JSON_SCHEMA = {
    "name": "voicemail_spam_classification",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "is_spam",
            "confidence",
            "callback_number",
            "claimed_company",
            "scam_category",
            "summary",
            "ftc_comment",
            "should_report",
        ],
        "properties": {
            "is_spam": {"type": "boolean"},
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "callback_number": {
                "type": ["string", "null"],
                "description": "+1XXXXXXXXXX form, or null if absent.",
            },
            "claimed_company": {
                "type": ["string", "null"],
                "description": "Name caller used for themselves, or null.",
            },
            "scam_category": {
                "type": "string",
                "enum": SCAM_CATEGORIES,
            },
            "summary": {
                "type": "string",
                "description": "<=200 chars internal summary.",
            },
            "ftc_comment": {
                "type": "string",
                "description": "Prose to paste into the FTC complaint form. <=900 chars.",
            },
            "should_report": {"type": "boolean"},
        },
    },
    "strict": True,
}


def build_user_prompt(caller_number: str | None, transcript: str) -> str:
    parts = []
    parts.append("Caller number: " + (caller_number or "(unknown)"))
    parts.append("Transcript:")
    parts.append(transcript.strip() or "(no transcript available)")
    parts.append("")
    parts.append(
        "Return the JSON object now. Remember: be conservative on personal "
        "messages and aggressive on clear robocalls."
    )
    return "\n".join(parts)
