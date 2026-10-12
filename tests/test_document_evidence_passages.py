"""Bounded document evidence preserves source text, privacy and honest coverage."""

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from knowledge_layer.connectors import local_files
from knowledge_layer.contracts import search_document_evidence
from knowledge_layer.store import KnowledgeStore


def _registry(tmp_path, *, retain=True):
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    connection = store.create_connection("test", kind="local_files", name="Policies", config={
        "path": ".", "retain_semantic_samples": retain,
    })
    snapshot = store.refresh_connection("test", connection["id"])["snapshot"]
    return store, connection, snapshot


def _located_text(lines, locator):
    first = locator["extracted_line_start"] - 1
    last = locator["extracted_line_end"] - 1
    left, right = locator["column_start"] - 1, locator["column_end"]
    if first == last:
        return lines[first][left:right]
    return lines[first][left:] + "".join(lines[first + 1:last]) + lines[last][:right]


def _pdf(pages):
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 0 0 Td (Policy page {index + 1}: controls stay frozen.) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_later_paragraph_is_retrievable_with_exact_revision_bound_locator(tmp_path):
    body = "# Operations\n\nOpening context only.\n\n## Decision\n\nFirst paragraph.\n\n" \
           "Vendor projections stay as frozen benchmark control arms, never organic features.\n" \
           "Synthetic forecast controls require an independent review.\n"
    (tmp_path / "policy.md").write_text(body)
    store, _, snapshot = _registry(tmp_path)
    try:
        result = search_document_evidence(store, tenant_id="test", query="Synthetic forecast", limit=5)
        assert result["total_matches"] == 1
        match = result["matches"][0]
        assert "frozen benchmark control arms" in match["excerpt"]
        assert match["source_revision"] == snapshot["assets"][0]["revision"]
        assert _located_text(body.splitlines(keepends=True), match["source_locator"]) == match["excerpt"]
        assert match["source_locator"]["line_start"] == 9
        assert match["source_locator"]["line_end"] == 10
        assert match["extraction_complete"] is True
        assert match["retention_complete"] is True
        assert match["evidence_ref"]["snapshot_id"] == snapshot["id"]
        assert all(item["status"] == "candidate" for item in snapshot["concepts"])
    finally:
        store.close()


@pytest.mark.parametrize("query", [
    "Please tell me about frozen controls",
    "Tell me about frozen controls",
    "What do we know about frozen controls?",
    "What can you tell me about frozen controls?",
])
def test_conversational_question_retrieves_the_same_revision_bound_passage(tmp_path, query):
    body = "# Policy\n\nFrozen controls require review.\n"
    (tmp_path / "policy.md").write_text(body)
    store, _, snapshot = _registry(tmp_path)
    try:
        direct = search_document_evidence(store, tenant_id="test", query="frozen controls")
        question = search_document_evidence(store, tenant_id="test", query=query)
        assert question["matches"] == direct["matches"]
        assert question["total_matches"] == 1
        match = question["matches"][0]
        assert _located_text(body.splitlines(keepends=True), match["source_locator"]) == match["excerpt"]
        assert match["source_revision"] == snapshot["assets"][0]["revision"]
        assert match["evidence_ref"]["snapshot_id"] == snapshot["id"]
        assert match["match_type"] == "full"
        assert all(item["status"] == "candidate" for item in snapshot["concepts"])
    finally:
        store.close()


@pytest.mark.parametrize("suffix", [".md", ".txt"])
def test_long_line_and_headingless_text_keep_late_words_and_exact_chunks(suffix):
    body = ("Long policy introduction " * 55) + "eventual escrow policy applies.\n"
    path = Path("generic" + suffix)
    profile = local_files._document_profile(path, body.encode(), True)
    descriptions = profile["document_signals"]["candidate_descriptions"]
    assert any("eventual escrow" in item["excerpt"] for item in descriptions)
    assert profile["document_retention"]["complete"] is True
    for item in descriptions:
        assert len(item["excerpt"]) <= local_files.MAX_DOCUMENT_CHUNK_CHARS
        assert _located_text(body.splitlines(keepends=True), item["source_locator"]) == item["excerpt"]
    assert profile["document_signals"]["candidate_term_locators"]["generic"]["coordinate_basis"] == "source_filename"


def test_html_later_paragraphs_h6_and_entity_newlines_have_honest_coordinates():
    body = "<h6>Escrow policy</h6>\n<p>Opening context.</p>\n" \
           "<p>Later frozen&#10;controls survive &amp; require review.</p>\n" \
           "<script>secret script body</script><style>secret style body</style>"
    path = Path("policy.html")
    profile = local_files._document_profile(path, body.encode(), True)
    lines, status = local_files._document_lines(path, body.encode())
    assert status == "extracted"
    descriptions = profile["document_signals"]["candidate_descriptions"]
    later = next(item for item in descriptions if "Later frozen" in item["excerpt"])
    assert "controls survive & require review" in later["excerpt"]
    locator = later["source_locator"]
    assert locator["coordinate_basis"] == "html_source_node_start_line_and_rendered_text_line"
    assert locator["source_node_line_start"] == 3
    assert locator["source_node_line_end"] == 3
    assert "line_start" not in locator
    assert _located_text(lines, locator) == later["excerpt"]
    assert "secret" not in json.dumps(profile)


def test_pdf_text_has_page_coordinates_and_page_budget_is_explicit():
    path = Path("policies.pdf")
    data = _pdf(9)
    profile = local_files._document_profile(path, data, True)
    lines, _ = local_files._document_lines(path, data)
    descriptions = profile["document_signals"]["candidate_descriptions"]
    assert descriptions
    for item in descriptions:
        locator = item["source_locator"]
        assert locator["coordinate_basis"] == "pdf_page_and_extracted_text_line"
        assert 1 <= locator["page_start"] <= locator["page_end"] <= 8
        assert _located_text(lines, locator) == item["excerpt"]
    assert descriptions[-1]["source_locator"]["page_end"] == 8
    assert "Policy page 9" not in json.dumps(descriptions)
    assert profile["document_extraction"]["complete"] is False
    assert profile["document_extraction"]["truncation_reasons"] == ["pdf_page_limit"]
    assert profile["document_retention"]["complete"] is False


def test_scanned_pdf_never_claims_complete_text_or_ocr():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    profile = local_files._document_profile(Path("scan.pdf"), output.getvalue(), True)
    assert profile["extraction_status"] == "no_text_or_ocr_required"
    assert profile["document_extraction"]["complete"] is False
    assert profile["document_extraction"]["truncated"] is False
    assert profile["document_retention"]["complete"] is False
    assert profile["document_signals"]["candidate_descriptions"] == []


def test_retention_off_preserves_only_heading_metadata_no_body_or_threshold(tmp_path):
    body = "# Dispatch policy\n\nConfidential escrow body. Complete within 17 minutes.\n"
    (tmp_path / "policy.md").write_text(body)
    store, _, snapshot = _registry(tmp_path, retain=False)
    try:
        encoded = json.dumps(snapshot)
        assert "Confidential escrow" not in encoded
        assert '"value": 17' not in encoded
        asset = snapshot["assets"][0]
        assert asset["profile"]["document_retention"]["enabled"] is False
        assert asset["profile"]["document_retention"]["complete"] is None
        assert asset["profile"]["document_signals"]["quantified_claims"] == []
        assert search_document_evidence(store, tenant_id="test", query="escrow")["matches"] == []
        heading = search_document_evidence(store, tenant_id="test", query="Dispatch policy")["matches"][0]
        assert heading["excerpt"] is None
        assert heading["source_locator"]["line_start"] == 1
    finally:
        store.close()


def test_retention_budget_keeps_inventory_separate_from_partial_curation(tmp_path):
    (tmp_path / "policy.md").write_text("# Decision\n\n" + "policy context. " * 2000)
    store, connection, snapshot = _registry(tmp_path)
    try:
        profile = snapshot["assets"][0]["profile"]
        assert profile["document_extraction"]["complete"] is True
        retention = profile["document_retention"]
        assert retention["truncated"] is True and retention["complete"] is False
        assert retention["retained_chunks"] == local_files.MAX_SEMANTIC_SAMPLES
        assert snapshot["discovery_complete"] is True
        assert snapshot["curation_complete"] is False
        assert snapshot["curation_status"] == "partial_curation"
        assert "curation_incomplete" in store.publication_preview("test", connection["id"])["blockers"]
    finally:
        store.close()


@pytest.mark.parametrize("budget,body,reason", [
    ("MAX_DOCUMENT_TEXT_CHARS", "Body content after text budget.", "text_character_limit"),
    ("MAX_DOCUMENT_LINES", "one\ntwo\nthree\nfour\n", "text_line_limit"),
])
def test_extraction_budgets_have_explicit_incomplete_state(monkeypatch, budget, body, reason):
    monkeypatch.setattr(local_files, budget, 3)
    profile = local_files._document_profile(Path("policy.txt"), body.encode(), True)
    assert profile["document_extraction"]["complete"] is False
    assert reason in profile["document_extraction"]["truncation_reasons"]
    assert profile["document_retention"]["complete"] is False


def test_empty_bounded_prefix_is_not_a_complete_empty_document(monkeypatch):
    monkeypatch.setattr(local_files, "MAX_DOCUMENT_TEXT_CHARS", 3)
    profile = local_files._document_profile(Path("policy.txt"), b"   Unexamined policy text", True)
    assert profile["extraction_status"] == "no_text"
    assert profile["document_extraction"]["truncated"] is True
    assert profile["document_extraction"]["complete"] is False
    assert profile["document_retention"]["complete"] is False


@pytest.mark.parametrize("retain", [True, False])
def test_quantified_candidate_limit_does_not_claim_complete_text_was_incomplete(tmp_path, retain):
    body = "# Service deadline\n\n" + "Complete within 1 minutes.\n" * 100 + "Complete within 99 minutes.\n"
    (tmp_path / "policy.md").write_text(body)
    store, connection, snapshot = _registry(tmp_path, retain=retain)
    try:
        profile = snapshot["assets"][0]["profile"]
        assert profile["document_extraction"]["complete"] is True
        assert profile["document_retention"]["complete"] is (True if retain else None)
        candidates = profile["document_candidates"]
        assert candidates["complete"] is (not retain)
        assert candidates["truncated"] is retain
        assert candidates["observed_quantified_claims"] == (101 if retain else None)
        assert candidates["retained_quantified_claims"] == (100 if retain else 0)
        assert len(profile["document_signals"]["quantified_claims"]) == (100 if retain else 0)
        assert snapshot["discovery_complete"] is True
        assert snapshot["curation_complete"] is (not retain)
        blockers = store.publication_preview("test", connection["id"])["blockers"]
        assert ("curation_incomplete" in blockers) is retain
    finally:
        store.close()


def test_all_claim_occurrences_on_one_line_count_toward_candidate_budget():
    body = "# Service deadline\n" + "under 1 minutes; " * 101
    profile = local_files._document_profile(Path("policy.md"), body.encode(), True)
    assert profile["document_candidates"]["observed_quantified_claims"] == 101
    assert profile["document_candidates"]["truncated"] is True
    assert profile["document_extraction"]["complete"] is True


def test_heading_candidate_limit_has_its_own_coverage_state():
    body = "".join(f"# Heading {index}\n\n" for index in range(51))
    profile = local_files._document_profile(Path("policy.md"), body.encode(), False)
    assert profile["document_extraction"]["complete"] is True
    assert profile["document_extraction"]["truncated"] is False
    assert profile["document_candidates"]["complete"] is False
    assert profile["document_candidates"]["truncation_reasons"] == ["heading_candidate_limit"]
    assert len(profile["document_signals"]["candidate_terms"]) == local_files.MAX_SEMANTIC_SAMPLES


@pytest.mark.parametrize("suffix", [".md", ".txt", ".html"])
def test_invalid_utf8_refuses_substituted_evidence(tmp_path, suffix):
    (tmp_path / ("invalid" + suffix)).write_bytes(b"# Policy\nBody \xff changed by replacement.")
    result = local_files.discover({"root": str(tmp_path), "path": ".", "retain_semantic_samples": True})
    assert result["errors"][0]["code"] == "document_decode_error"
    assert result["assets"][0]["profile"]["content_retained"] is False
    assert "document_signals" not in result["assets"][0]["profile"]


def test_document_revision_invalidates_unchanged_passage_handles(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text("# Policy\n\nFrozen controls require review.\n\nOther first text.\n")
    store, connection, first = _registry(tmp_path)
    try:
        before = search_document_evidence(store, tenant_id="test", query="Frozen controls")["matches"][0]
        path.write_text("# Policy\n\nFrozen controls require review.\n\nOther revised text.\n")
        second = store.refresh_connection("test", connection["id"])["snapshot"]
        after = search_document_evidence(store, tenant_id="test", query="Frozen controls")["matches"][0]
        assert before["excerpt"] == after["excerpt"]
        assert before["evidence_ref"]["evidence_id"] != after["evidence_ref"]["evidence_id"]
        assert before["source_revision"] != after["source_revision"]
        assert first["id"] != second["id"]
    finally:
        store.close()


def test_multi_term_query_does_not_return_generic_identity_deployment_comment(tmp_path):
    (tmp_path / "policy.md").write_text(
        "# Deployment\n\nGoogle identity tokens authenticate service requests.\n\n"
        "# Identity policy\n\nsynthetic_account_resolver is the account identity engine. CRM and billing IDs stay distinct.\n"
    )
    store, _, _ = _registry(tmp_path)
    try:
        result = search_document_evidence(store, tenant_id="test", query="account identity")
        assert result["total_matches"] == 1
        assert "synthetic_account_resolver" in result["matches"][0]["excerpt"]
        assert result["search_mode"] == "lexical_all_query_terms"
    finally:
        store.close()


def test_legacy_evidence_coverage_and_locator_remain_unknown():
    source = {"connection_id": "legacy", "connection_name": "Legacy", "snapshot_id": "snapshot",
              "evidence": [{"id": "term", "evidence_type": "document_derived_term",
                            "summary": "Frozen controls", "details": {"term": "Frozen controls"}}]}
    store = SimpleNamespace(iter_current_evidence_sources=lambda _: iter([source]))
    result = search_document_evidence(store, tenant_id="test", query="Frozen controls")
    assert result["unknown_text_coverage_evidence"] == 1
    match = result["matches"][0]
    assert match["source_locator"] is None
    assert match["extraction_complete"] is None
    assert match["retention_complete"] is None
