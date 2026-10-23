"""Conservative local detection of likely sensitive document content."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, List


@dataclass(frozen=True)
class SensitiveFinding:
    kind: str
    signals: tuple[str, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["signals"] = list(self.signals)
        return value


AADHAAR_PATTERN = re.compile(r"(?<!\d)([2-9]\d{3}[ -]?\d{4}[ -]?\d{4})(?!\d)")
PAN_PATTERN = re.compile(r"(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])")
CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
API_KEY_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
FINANCIAL_TERMS = (
    "invoice", "receipt", "amount due", "tax invoice", "bank account",
    "account number", "payment", "balance", "transaction", "salary slip",
)


def luhn_valid(value: str) -> bool:
    digits = [int(character) for character in value if character.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def detect_sensitive(text: str, metadata: Iterable[str] = ()) -> List[SensitiveFinding]:
    """Return likely-sensitive categories without retaining matched values."""
    haystack = " ".join([text or "", *map(str, metadata)])
    findings: List[SensitiveFinding] = []
    lowered = haystack.lower()
    for match in AADHAAR_PATTERN.finditer(haystack):
        value = match.group(1)
        signals = []
        if re.fullmatch(r"\d{4}[ -]\d{4}[ -]\d{4}", value):
            signals.append("4-4-4 grouping")
        if any(term in lowered for term in ("aadhaar", "uidai", "government id")):
            signals.append("identity context")
        if len(signals) >= 2:
            findings.append(SensitiveFinding("likely_aadhaar", tuple(signals)))
            break
    if PAN_PATTERN.search(haystack):
        signals = ["PAN format"]
        if any(term in lowered for term in ("income tax", "tax department", "permanent account")):
            signals.append("tax identity context")
        findings.append(SensitiveFinding("likely_pan", tuple(signals)))

    card_context = any(
        term in lowered
        for term in ("card number", "credit card", "debit card", "payment card", "cvv", "expires")
    )
    for match in CARD_CANDIDATE.finditer(haystack):
        if luhn_valid(match.group(0)):
            signals = ["Luhn valid"]
            if card_context:
                signals.append("payment-card context")
            elif len(re.sub(r"\D", "", match.group(0))) >= 16:
                signals.append("16+ digit number")
            findings.append(SensitiveFinding("likely_payment_card", tuple(signals)))
            break

    if any(pattern.search(text or "") for pattern in API_KEY_PATTERNS):
        findings.append(SensitiveFinding("likely_api_key", ("provider key pattern",)))
    if PRIVATE_KEY_PATTERN.search(text or ""):
        findings.append(SensitiveFinding("private_key", ("PEM private-key header",)))

    matches = sum(1 for term in FINANCIAL_TERMS if term in lowered)
    if matches >= 2:
        findings.append(
            SensitiveFinding(
                "likely_financial_document",
                (f"{matches} financial terms",),
            )
        )
    return findings


def redact_sensitive(text: str) -> str:
    """Mask likely identifiers and credentials; exact matches never enter diagnostics."""
    result = text or ""
    result = PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY HEADER]", result)
    for pattern in API_KEY_PATTERNS:
        result = pattern.sub("[REDACTED API KEY]", result)
    result = PAN_PATTERN.sub("[REDACTED PAN]", result)
    result = AADHAAR_PATTERN.sub(
        lambda match: "[REDACTED AADHAAR]" if len(re.findall(r"\d", match.group(0))) == 12 else match.group(0),
        result,
    )

    def card(match: re.Match) -> str:
        return "[REDACTED PAYMENT CARD]" if luhn_valid(match.group(0)) else match.group(0)

    return CARD_CANDIDATE.sub(card, result)
