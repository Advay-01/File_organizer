"""File relationships are typed evidence, never authorization to move a file."""
from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.core.evidence import (
    EvidenceBundle,
    _document_metadata,
    _xmp_relationship_metadata,
    extract_evidence,
)
from app.core.relationships import local_resource_missing, relationship_groups
from app.models.schemas import FileInfo


def test_resource_absence_is_checked_without_reading_contents_or_resolving_links(tmp_path):
    with patch('pathlib.Path.open', side_effect=AssertionError('Do not read assets')):
        assert local_resource_missing(tmp_path / 'page.html', 'missing/style.css', tmp_path)
        asset = tmp_path / 'style.css'
        asset.touch()
        assert not local_resource_missing(tmp_path / 'page.html', 'style.css', tmp_path)


@pytest.mark.parametrize('reference', ['../outside.css', '//server/share.css', 'https://example.invalid/style.css', r'C:\outside.css'])
def test_unknown_or_external_resources_are_not_inspected(tmp_path, reference):
    with patch('pathlib.Path.lstat', side_effect=AssertionError('Do not inspect outside the root')):
        assert not local_resource_missing(tmp_path / 'page.html', reference, tmp_path)


def test_inaccessible_and_redirected_resources_keep_the_strict_guard(tmp_path):
    with patch('pathlib.Path.lstat', side_effect=PermissionError):
        assert not local_resource_missing(tmp_path / 'page.html', 'style.css', tmp_path)
    with patch('pathlib.Path.lstat', return_value=SimpleNamespace(st_mode=stat.S_IFLNK)):
        assert not local_resource_missing(tmp_path / 'page.html', 'style.css', tmp_path)

def bundle(path="/chosen/file.txt", content="ordinary evidence", metadata=None, **kwargs):
    return EvidenceBundle(
        path=path, fingerprint=hashlib.sha256(content.encode()).hexdigest(),
        filename=Path(path).name, extension=Path(path).suffix,
        parent_directory=Path(path).parent.name, content=content,
        metadata=metadata or {}, extraction_status="sufficient", **kwargs,
    )


def file_info(path):
    return FileInfo(path=str(path), filename=path.name, extension=path.suffix,
                    size_kb=1, date_created="2026-01-01")


def test_numbered_scans_with_distinct_subjects_are_not_related():
    titles = ["Thermodynamics Lecture", "Dental Treatment", "Equipment Invoice", "Travel Itinerary"]
    evidence = {index: bundle(f"/chosen/scan{index:03d}.pdf", content=title,
                              metadata={"title": title, "author": "Shared scanner"})
                for index, title in enumerate(titles)}
    assert relationship_groups(evidence) == []


def test_same_name_author_and_timestamps_in_different_folders_do_not_link():
    evidence = {
        0: bundle("/chosen/work/notes.pdf", "Invoice", {"author": "Someone", "modified": "2026-01-01"}),
        1: bundle("/chosen/course/notes.pdf", "Physics", {"author": "Someone", "modified": "2026-01-01"}),
    }
    assert relationship_groups(evidence) == []


def test_duplicates_form_a_linear_star_and_keep_individual_file_ids():
    evidence = {index: bundle(f"/chosen/{index}.txt") for index in range(10)}
    edges = relationship_groups(evidence)
    assert len(edges) == 9
    assert {edge.source_id for edge in edges} == set(range(1, 10))
    assert {edge.target_id for edge in edges} == {0}
    assert {edge.kind for edge in edges} == {"duplicate"}
    assert {edge.strength for edge in edges} == {"verified"}
    assert len(evidence) == 10


def test_sensitive_evidence_is_not_exposed_by_relationship_hints():
    sensitive = bundle("/chosen/credentials.txt", sensitive_findings=[{"kind": "credentials"}])
    assert relationship_groups({0: bundle(), 1: sensitive}) == []


def test_malformed_fingerprints_never_prove_a_duplicate():
    a, b = bundle(), bundle("/chosen/copy.txt")
    a.fingerprint = b.fingerprint = "not-a-content-hash"
    assert relationship_groups({0: a, 1: b}) == []


def test_matching_document_id_needs_distinct_instance_ids_for_a_version_candidate():
    evidence = {
        0: bundle(content="first", metadata={"xmp_document_id": "uuid:document", "xmp_instance_id": "uuid:first"}),
        1: bundle("/chosen/revised.pdf", "second", {"xmp_document_id": "uuid:document", "xmp_instance_id": "uuid:second"}),
    }
    edges = relationship_groups(evidence)
    assert [(edge.kind, edge.strength) for edge in edges] == [("possible_version", "candidate")]


@pytest.mark.parametrize("instance", [None, "uuid:same"])
def test_repeated_or_missing_instance_id_cannot_prove_versions(instance):
    metadata = {"xmp_document_id": "uuid:copied"}
    if instance:
        metadata["xmp_instance_id"] = instance
    assert relationship_groups({0: bundle(content="physics", metadata=metadata),
                                1: bundle("/chosen/other.pdf", "invoice", metadata)}) == []


def test_explicit_derived_from_names_only_the_selected_parent_instance():
    evidence = {
        0: bundle(content="first", metadata={"xmp_document_id": "uuid:parent", "xmp_instance_id": "uuid:one"}),
        1: bundle("/chosen/revised.txt", "second", {"xmp_document_id": "uuid:parent", "xmp_instance_id": "uuid:two"}),
        2: bundle("/chosen/export.pdf", "export", {"xmp_document_id": "uuid:export", "xmp_instance_id": "uuid:export-one",
                    "xmp_derived_from_document_id": "uuid:parent", "xmp_derived_from_instance_id": "uuid:two"}),
    }
    edges = [edge for edge in relationship_groups(evidence) if edge.kind == "derivation"]
    assert [(edge.source_id, edge.target_id, edge.strength) for edge in edges] == [(2, 1, "asserted")]


def test_ambiguous_parent_ids_do_not_invent_a_direct_parent():
    evidence = {
        0: bundle(content="first", metadata={"xmp_document_id": "uuid:parent"}),
        1: bundle("/chosen/revised.txt", "second", {"xmp_document_id": "uuid:parent"}),
        2: bundle("/chosen/export.pdf", "export", {"xmp_derived_from_document_id": "uuid:parent"}),
    }
    assert relationship_groups(evidence) == []


def test_original_document_id_is_an_asserted_derivation_hint():
    evidence = {
        0: bundle(metadata={"xmp_document_id": "uuid:original"}),
        1: bundle("/chosen/export.pdf", "export", {"xmp_document_id": "uuid:export", "xmp_original_document_id": "uuid:original"}),
    }
    edges = relationship_groups(evidence)
    assert [(edge.source_id, edge.target_id, edge.kind, edge.strength) for edge in edges] == [(1, 0, "derivation", "asserted")]


def test_conflicting_embedded_ids_are_ignored_without_losing_verified_duplicates():
    a = bundle(metadata={"xmp_document_id": "uuid:one", "xmp_instance_id": "uuid:a", "xmp_relationship_conflict": True})
    b = bundle("/chosen/b.txt", metadata={"xmp_document_id": "uuid:one", "xmp_instance_id": "uuid:b"})
    assert [edge.kind for edge in relationship_groups({0: a, 1: b})] == ["duplicate"]


def test_html_dependencies_only_resolve_already_selected_files_and_perform_no_io():
    evidence = {
        0: bundle("/chosen/pages/index.html", "HTML", {"local_dependencies": [
            "../images/logo.png", "https://example.invalid/file", "file:///secret.txt",
            "/private/secret.txt", "../../outside.txt", "logo.png?remote=1", "\\\\server\\share.png",
        ]}),
        1: bundle("/chosen/images/logo.png", "image"),
    }
    with patch("pathlib.Path.open", side_effect=AssertionError("No referenced file may be opened")):
        edges = relationship_groups(evidence)
    assert [(edge.source_id, edge.target_id, edge.kind) for edge in edges] == [(0, 1, "dependency")]


def test_xmp_sidecar_needs_matching_ids_and_neighboring_name_to_be_a_companion():
    metadata = {"xmp_document_id": "uuid:photo", "xmp_instance_id": "uuid:current"}
    image = bundle("/chosen/photo.jpg", "image", metadata)
    sidecar = bundle("/chosen/photo.xmp", "metadata", metadata)
    edges = relationship_groups({0: image, 1: sidecar})
    assert [(edge.kind, edge.strength) for edge in edges] == [("dependency", "candidate")]
    assert relationship_groups({0: image, 1: bundle("/chosen/other.xmp", "metadata", metadata)}) == []
    assert relationship_groups({0: image, 1: bundle("/other/photo.xmp", "metadata", metadata)}) == []
    assert relationship_groups({0: image, 1: bundle("/chosen/photo.xmp", "metadata", {})}) == []


def test_large_reused_metadata_ids_do_not_chain_a_whole_directory_together():
    evidence = {index: bundle(f"/chosen/{index}.pdf", f"file {index}",
                              {"xmp_document_id": "uuid:template", "xmp_instance_id": f"uuid:instance-{index}"})
                for index in range(65)}
    assert relationship_groups(evidence) == []


def xmp_packet(attributes="", body=""):
    return f'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
      xmlns:stRef="http://ns.adobe.com/xap/1.0/sType/ResourceRef#" {attributes}>
    {body}</rdf:Description></rdf:RDF></x:xmpmeta>'''.encode()


def test_xmp_attribute_and_element_forms_extract_only_relationship_identifiers():
    raw = xmp_packet('xmpMM:DocumentID="uuid:document" xmpMM:InstanceID="uuid:instance"', '''
    <xmpMM:OriginalDocumentID>uuid:original</xmpMM:OriginalDocumentID>
    <xmpMM:DerivedFrom rdf:parseType="Resource" stRef:documentID="uuid:parent">
      <stRef:instanceID>uuid:parent-instance</stRef:instanceID>
    </xmpMM:DerivedFrom>''')
    assert _xmp_relationship_metadata(raw) == {
        "xmp_document_id": "uuid:document", "xmp_instance_id": "uuid:instance",
        "xmp_original_document_id": "uuid:original", "xmp_derived_from_document_id": "uuid:parent",
        "xmp_derived_from_instance_id": "uuid:parent-instance",
    }


@pytest.mark.parametrize("raw", [b"<broken", b"x" * (256 * 1024 + 1),
    b'<!DOCTYPE x [<!ENTITY external SYSTEM "file:///secret">]><x>&external;</x>',
    '<!DOCTYPE x [<!ENTITY local "uuid:injected">]><x>&local;</x>'.encode("utf-16"),
    b'<?xml version="1.0" encoding="x-invalid"?><x/>'],
    ids=["malformed", "oversized", "entity-declaration", "utf16-entity", "unknown-encoding"])
def test_malformed_oversized_or_entity_defining_xmp_is_not_processed(raw):
    assert _xmp_relationship_metadata(raw) == {}


def test_conflicting_xmp_ids_are_recorded_without_selecting_one():
    result = _xmp_relationship_metadata(xmp_packet('xmpMM:DocumentID="uuid:one"',
                                                   '<xmpMM:DocumentID>uuid:two</xmpMM:DocumentID>'))
    assert "xmp_relationship_conflict" in result
    assert "xmp_document_id" not in result


def test_embedded_pdf_xmp_is_extracted_without_changing_the_file(tmp_path):
    path = tmp_path / "document.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "Physics Lecture Notes", "/Creator": "Fixture"})
    writer.xmp_metadata = xmp_packet('xmpMM:DocumentID="uuid:document" xmpMM:InstanceID="uuid:instance"')
    with path.open("wb") as output:
        writer.write(output)
    before = path.read_bytes()
    metadata = _document_metadata(path, ".pdf")
    assert metadata["title"] == "Physics Lecture Notes"
    assert metadata["xmp_document_id"] == "uuid:document"
    assert metadata["xmp_instance_id"] == "uuid:instance"
    assert path.read_bytes() == before


def test_html_resource_relationships_are_metadata_not_fetched(tmp_path):
    path = tmp_path / "index.html"
    path.write_text('''<title>Physics Notes</title><img src="figures/chart.png">
        <link rel="stylesheet" href="style.css"><a href="https://example.invalid">remote</a>''')
    metadata = _document_metadata(path, ".html")
    assert metadata["local_dependencies"] == ["figures/chart.png", "style.css"]
    assert metadata["title"] == "Physics Notes"


@pytest.mark.parametrize("title", [None, "Untitled", "Document1", "Microsoft Word"])
def test_blank_pdf_creator_and_generic_title_are_not_semantic_evidence(tmp_path, title):
    path = tmp_path / "scan.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
    metadata = {"/Creator": "An expensive scanner", "/Author": "Someone"}
    if title:
        metadata["/Title"] = title
    writer.add_metadata(metadata)
    with path.open("wb") as output:
        writer.write(output)
    with patch("app.core.evidence.shutil.which", return_value=None):
        evidence = extract_evidence(file_info(path))
    assert evidence.extraction_status == "unavailable"
    assert not evidence.ocr_used
    assert any("OCR" in warning for warning in evidence.warnings)


def test_real_image_dimensions_are_not_semantic_content(tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (1600, 1200), color="white").save(path)
    with patch("app.core.evidence.shutil.which", return_value=None):
        evidence = extract_evidence(file_info(path))
    assert evidence.metadata["dimensions"] == "1600x1200"
    assert evidence.content == ""
    assert evidence.extraction_status == "unavailable"


def test_meaningful_title_without_text_stays_weak_and_needs_review(tmp_path):
    path = tmp_path / "scan.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "Thermodynamics Lecture Notes"})
    with path.open("wb") as output:
        writer.write(output)
    with patch("app.core.evidence.shutil.which", return_value=None):
        evidence = extract_evidence(file_info(path))
    assert evidence.extraction_status == "weak"
    assert evidence.metadata["title"] == "Thermodynamics Lecture Notes"


def test_optional_pdf_ocr_is_bounded_and_uses_only_local_tools(tmp_path):
    from app.core.evidence import _ocr_pdf

    source = tmp_path / "scan.pdf"; source.write_bytes(b"fixture")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["timeout"] <= 15
        assert "shell" not in kwargs
        prefix = Path(command[-1])
        Image.new("RGB", (32, 32), "white").save(prefix.with_name(prefix.name + "-1.png"))
        return SimpleNamespace(returncode=0)

    with (patch("app.core.evidence.shutil.which", side_effect=lambda name: f"local-{name}"),
          patch("app.core.evidence.subprocess.run", side_effect=run),
          patch("app.core.evidence._ocr_image", return_value=("Grounded OCR words", True))):
        text, used = _ocr_pdf(source, 100)
    assert text == "Grounded OCR words" and used
    command = calls[0][0]
    assert command[command.index("-l") + 1] == "2"
    assert command[command.index("-scale-to") + 1] == "1600"
    assert source.read_bytes() == b"fixture"
