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
# A decimal's fractional digits can coincidentally pass Luhn (for example
# e = 2.7182818284590452). Only match standalone numeric tokens, never part
# of a decimal, scientific notation, or an alphanumeric identifier.
CARD_CANDIDATE = re.compile(r"(?<![\w.])\d(?:[ -]?\d){12,18}(?!\w|\.\d)")
CARD_CONTEXT = re.compile(
    r"\b(?:credit card|debit card|payment card|card number|card details|cardholder|cvv|visa|mastercard|amex)\b"
    r"|\bcard\s*[:#=]?\s*\(?\s*\d",
    re.I,
)
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
PERSONAL_FINANCIAL_LABELS = (
    "bank statement", "account holder", "statement period", "opening balance",
    "closing balance", "salary slip", "net pay", "employee id",
)
FINANCIAL_IDENTIFIER = re.compile(
    r"\b(?:account\s*(?:number|no\.?|#)|bank\s+account|routing\s*(?:number|no\.?)|iban|ifsc)"
    r"\s*[:=#-]?\s*(?=[A-Z0-9* -]{4,40}(?:\b|$))"
    r"[A-Z* -]*\d[A-Z0-9* -]{2,35}",
    re.I,
)


_SHELL = r"(?:sudo\s+)?(?:ba|z|da|k)?sh\b"
_DOWNLOAD_AND_RUN = re.compile(
    rf"\b(?:curl|wget)\b[^\r\n|;&]*\|\s*{_SHELL}",
    re.I,
)
_DOWNLOAD_TO_FILE = re.compile(
    rf"\b(?:curl|wget)\b[^\r\n;&|]*(?:\s-o\s+|\s--output(?:-document)?\s+)"
    r"(?P<file>[^\s;&|'\"]+)[^\r\n;&|]*(?:&&|;)[^\r\n]*"
    rf"(?:\b(?:ba|z|da|k)?sh\s+(?P=file)\b|"
    rf"\bchmod\s+\+x\s+(?P=file)\b|"
    rf"(?:&&|;)\s*(?:\./)?(?P=file)\b)",
    re.I,
)
_DOWNLOAD_AND_RUN_POWERSHELL = re.compile(
    r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\r\n|]*"
    r"\|\s*(?:iex|invoke-expression)\b"
    r"|\b(?:iex|invoke-expression)\b[^\r\n]*"
    r"\b(?:downloadstring|iwr|irm|invoke-webrequest|invoke-restmethod)\b",
    re.I,
)
_ENCODED_POWERSHELL = re.compile(
    r"\bpowershell(?:\.exe)?\b[^\r\n]*(?<!\w)"
    r"-(?:e|ec|enc|encodedcommand)\b\s*:?\s*['\"]?[A-Za-z0-9+/=]{20,}",
    re.I,
)
_BASE64_TO_SHELL = re.compile(
    rf"\bbase64\s+(?:-d|--decode)\b[^\r\n|]*\|\s*{_SHELL}",
    re.I,
)
_BASE64_TO_POWERSHELL = re.compile(
    r"\b(?:iex|invoke-expression)\b[^\r\n]*"
    r"(?:frombase64string|convert\.frombase64string)"
    r"|(?:frombase64string|convert\.frombase64string)[^\r\n]*"
    r"\|\s*(?:iex|invoke-expression)\b",
    re.I,
)
_DESTRUCTIVE_UNIX_ROOT = re.compile(
    r"(?im)(?:^|[;&|]\s*|\bsudo\s+)\brm\s+"
    r"(?=[^\r\n;&|]*-[a-z]*r)(?=[^\r\n;&|]*-[a-z]*f)"
    r"[^\r\n;&|]*?(?:--no-preserve-root\s+)?"
    r"['\"]?(?:/|~|\$HOME|\$\{HOME\})['\"]?"
    r"(?:\*(?![\w./-])|\s*(?:;|&&|\|\||$))",
)
_DESTRUCTIVE_WINDOWS_ROOT = re.compile(
    r"(?im)\b(?:remove-item|ri|del|erase|rmdir|rd)\b"
    r"(?=[^\r\n;&|]*(?:-recurse\b|/s\b))"
    r"(?=[^\r\n;&|]*(?:-force\b|/q\b))"
    r"[^\r\n;&|]*?['\"]?"
    r"(?:[a-z]:[\\/]?|\$env:(?:systemdrive|userprofile)[\\/]?)"
    r"['\"]?(?:\s*(?:;|&&|\|\||$)|\s*\*)",
    re.I,
)
_REVERSE_SHELL = re.compile(
    rf"\b(?:ba|z|da|k)?sh\s+-i\b[^\r\n]*(?:/dev/tcp/|>&\s*\d)"
    r"|\b(?:nc|ncat|netcat)\b[^\r\n]*(?:\s-e\s+(?:/bin/)?(?:ba|z|da|k)?sh\b"
    r"|\s--exec\s+(?:/bin/)?(?:ba|z|da|k)?sh\b)"
    r"|\bsocat\b[^\r\n]*(?:tcp|exec:)[^\r\n]*(?:exec:)?(?:/bin/)?(?:ba|z|da|k)?sh\b",
    re.I,
)
SUSPICIOUS_COMMANDS = (
    ("downloads and runs remote code", _DOWNLOAD_AND_RUN),
    ("downloads and runs remote code", _DOWNLOAD_TO_FILE),
    ("downloads and runs remote code", _DOWNLOAD_AND_RUN_POWERSHELL),
    ("runs an encoded PowerShell command", _ENCODED_POWERSHELL),
    ("decodes hidden text and runs it", _BASE64_TO_SHELL),
    ("decodes hidden text and runs it", _BASE64_TO_POWERSHELL),
    ("deletes a whole system or home folder", _DESTRUCTIVE_UNIX_ROOT),
    ("deletes a whole system or home folder", _DESTRUCTIVE_WINDOWS_ROOT),
    ("opens a reverse shell", _REVERSE_SHELL),
)


def detect_suspicious_commands(text: str) -> List[str]:
    """Describe risky command patterns in plain words. Detection only: nothing is run or isolated."""
    value = text or ""
    found: List[str] = []
    for description, pattern in SUSPICIOUS_COMMANDS:
        if description not in found and pattern.search(value):
            found.append(description)
    return found


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

    metadata_card_context = bool(CARD_CONTEXT.search(" ".join(metadata_values).replace("_", " ")))
    for match in CARD_CANDIDATE.finditer(haystack):
        nearby = haystack[max(0, match.start() - 180):match.end() + 180]
        if luhn_valid(match.group(0)) and (metadata_card_context or CARD_CONTEXT.search(nearby)):
            signals = ["Luhn valid", "payment-card context"]
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

    matches = sum(1 for term in FINANCIAL_TERMS if re.search(r"\b" + re.escape(term) + r"\b", lowered))
    personal_labels = sum(1 for term in PERSONAL_FINANCIAL_LABELS if term in lowered)
    # Project proposals and lectures often discuss payments, receipts, and
    # balances. Require actual account details or multiple statement/payroll
    # fields before withholding them from topic classification.
    if FINANCIAL_IDENTIFIER.search(haystack) or (matches >= 2 and personal_labels >= 2):
        findings.append(
            SensitiveFinding(
                "likely_financial_document",
                ("personal account or statement details",),
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
