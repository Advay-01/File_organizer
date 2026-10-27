"""Scientific constants must reach the topic planner instead of sensitive review."""
import json
from unittest.mock import patch

import pytest

from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import plan_content_intelligence
from app.core.privacy import detect_sensitive, redact_sensitive
from app.core.evidence import extract_evidence
from app.core.extractor import ContentExtraction
from app.models.schemas import FileInfo


@pytest.mark.parametrize("formula", [
    "Euler constant e = 2.7182818284590452",
    "Square root of two = 1.4142135623730",
    "Natural logarithm ln(2) = 0.69314718055994530",
    "Experimental value = 7182818284590452e-16",
    "Experimental value = 7182818284590452E+3",
    "Sample identifier PHY7182818284590452",
])
def test_scientific_numbers_are_not_cards(formula):
    assert detect_sensitive(formula) == []
    assert redact_sensitive(formula) == formula


@pytest.mark.parametrize("text", [
    "Credit card number: 4111 1111 1111 1111",
    "Card: 4111111111111111.",
    "Card: (4111-1111-1111-1111)",
])
def test_real_card_patterns_remain_sensitive(text):
    assert "likely_payment_card" in {finding.kind for finding in detect_sensitive(text)}
    assert "4111" not in redact_sensitive(text)


def test_card_details_filename_provides_context():
    assert "likely_payment_card" in {
        finding.kind for finding in detect_sensitive("4111 1111 1111 1111", ["card_details.txt"])
    }


def test_checksum_alone_does_not_make_a_reference_number_sensitive():
    assert detect_sensitive("Experimental reference number 7182818284590452") == []


def test_pdf_internal_numbers_are_not_document_content(tmp_path):
    from pypdf import PdfWriter

    path = tmp_path / "Class_12_Physics_Formula_Sheet.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    with path.open("ab") as output:
        output.write(b"\n% Internal object identifier: 4111111111111111\n")
    file = FileInfo(path=str(path), filename=path.name, extension=".pdf", size_kb=1, date_created="")
    with patch("app.core.evidence.extract_content", return_value=ContentExtraction(
        text="Class 12 Physics Formula Sheet: F = ma, E = mc^2.", parser_status="ok",
        detected_type=".pdf", detection_confidence="high",
    )):
        assert not extract_evidence(file).sensitive


@pytest.mark.parametrize("text", [
    "Campus Expense Tracker Project Proposal. Track payment, receipt and balance features.",
    "ExpenseSplit hackathon pitch: receipt capture and payment splitting, balance calculations.",
    "Physics: charge balance and receipt of electromagnetic signals.",
])
def test_discussing_finance_is_not_personal_financial_data(text):
    assert detect_sensitive(text) == []


@pytest.mark.parametrize("text", [
    "Account holder: Example User. Statement period: September. Opening balance: 100. Closing balance: 50. Transaction history.",
    "Bank account number: 123456789012. IFSC: ABCD0123456",
    "Salary slip. Employee id: 1234. Net pay: 50000. Payment received.",
])
def test_personal_bank_and_payroll_documents_remain_sensitive(text):
    assert "likely_financial_document" in {finding.kind for finding in detect_sensitive(text)}


def test_physics_formulas_reach_planner_and_target_physics(tmp_path):
    path = tmp_path / "Physics_Formulas.txt"
    content = "Physics formulas for oscillations and wave motion. Euler constant e = 2.7182818284590452. " * 3
    path.write_text(content, encoding="utf-8")
    file = FileInfo(path=str(path), filename=path.name, extension=".txt", size_kb=1,
                    date_created="2026-10-26")
    calls = []

    def complete(messages):
        calls.append(messages)
        return json.dumps({"files": [{
            "id": 0, "category": "Physics", "subcategory": "Formula Sheets",
            "confidence": "high", "reasoning": "Physics formulas for oscillations and wave motion.",
            "evidence_quote": "Physics formulas for oscillations and wave motion",
            "filename_conflict": "none", "suggested_name": "", "name_evidence": "",
        }]})

    cache = ContentIntelligenceCache(tmp_path / "cache.db")
    try:
        for _ in range(2):
            result = plan_content_intelligence([file], str(tmp_path), ["fake/cloud-model"],
                                               cache=cache, completion_fn=complete)
            item = result.suggestions[0]
            assert not item.sensitive
            assert item.selected
            assert item.target_path == "Physics/Formula Sheets"
        assert len(calls) == 1  # Cached scans preserve the corrected result too.
        assert "2.7182818284590452" in json.dumps(calls[0])
    finally:
        cache.close()
