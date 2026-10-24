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
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)^(?P<prefix>[ \t]*(?:export[ \t]+)?)"
    r"(?P<key>[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY)"
    r"[A-Za-z0-9_.-]*)[ \t]*(?P<separator>[=:])[ \t]*"
    r"(?P<value>(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]*\S))[ \t]*\r?$"
)
SENSITIVE_FILENAME = re.compile(
    r"(?:^|[_.-])(?:credentials?|secrets?|passwords?|private[_-]?key|"
    r"api[_-]?key|access[_-]?token)(?:[_.-]|$)|(?:^|\.)env$",
    re.I,
)
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


def _credential_value_is_meaningful(match: re.Match) -> bool:
    value = match.group("value").strip()
    quoted = (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {'"', "'"}
    )
    if quoted:
        value = value[1:-1].strip()
    if not 4 <= len(value) <= 256:
        return False
    if re.fullmatch(
        r"(?i)(?:none|null|n/a|not set|redacted|placeholder|change[ _-]?me|"
        r"<[^>]+>|x{4,}|\.{3,})",
        value,
    ):
        return False
    if re.match(
        r"(?i)^(?:reset|required|change|enter|use|your|the|must|should)\b",
        value,
    ) and re.search(r"\s", value):
        return False
    if not quoted and re.search(r"\s", value):
        # Unquoted prose is ambiguous; quoted values may contain spaces.
        return False
    return bool(re.search(r"[A-Za-z0-9]", value))


def _credential_assignments(text: str):
    return (
        match
        for match in CREDENTIAL_ASSIGNMENT.finditer(text or "")
        if _credential_value_is_meaningful(match)
    )


def detect_sensitive(text: str, metadata: Iterable[str] = ()) -> List[SensitiveFinding]:
    """Return likely-sensitive categories without retaining matched values."""
    metadata_values = [str(value) for value in metadata]
    haystack = " ".join([text or "", *metadata_values])
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
    if next(_credential_assignments(text or ""), None) is not None:
        findings.append(
            SensitiveFinding("likely_credential_assignment", ("secret-like environment assignment",))
        )
    if any(SENSITIVE_FILENAME.search(value) for value in metadata_values):
        findings.append(
            SensitiveFinding("likely_credential_file", ("credential-like filename or context",))
        )

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
    def redact_assignment(match: re.Match) -> str:
        if not _credential_value_is_meaningful(match):
            return match.group(0)
        return (
            f"{match.group('prefix')}{match.group('key')}"
            f"{match.group('separator')} [REDACTED SECRET]"
        )

    result = CREDENTIAL_ASSIGNMENT.sub(redact_assignment, result)
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
