"""Common local document formats yield bounded evidence or explicit no-text state."""

import tracemalloc
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf._page import PageObject
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from knowledge_layer.connectors.local_files import _document_lines, discover


def test_html_docx_and_scanned_like_pdf_profiles(tmp_path):
    (tmp_path / "service.html").write_text(
        "<html><h2>Service window</h2><p>Coverage is Monday through Friday, 07:00 to 19:00 local time.</p>"
        "<script>ignore me</script></html>", encoding="utf-8"
    )
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:r><w:t>Billable hours</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Technician labor belongs to a work order.</w:t></w:r></w:p>'
        '</w:body></w:document>'
    )
    with zipfile.ZipFile(tmp_path / "labor.docx", "w") as archive:
        archive.writestr("word/document.xml", xml)
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with (tmp_path / "scan.pdf").open("wb") as handle:
        writer.write(handle)
    result = discover({"root": str(tmp_path), "path": ".", "retain_semantic_samples": True})
    assets = {item["name"]: item for item in result["assets"]}
    assert set(assets) == {"service.html", "labor.docx", "scan.pdf"}
    html_profile = assets["service.html"]["profile"]
    assert "Service window" in html_profile["document_signals"]["candidate_terms"]
    assert "Monday through Friday" in html_profile["document_signals"]["candidate_descriptions"][-1]["excerpt"]
    docx_profile = assets["labor.docx"]["profile"]
    assert "Billable hours" in docx_profile["document_signals"]["candidate_terms"]
    assert "Technician labor" in docx_profile["document_signals"]["candidate_descriptions"][-1]["excerpt"]
    pdf_profile = assets["scan.pdf"]["profile"]
    assert pdf_profile["extraction_status"] == "no_text_or_ocr_required"
    assert pdf_profile["document_signals"]["candidate_terms"] == []


def test_compact_pdf_with_large_decoded_content_stays_bounded(tmp_path, monkeypatch):
    import pypdf

    original_reader = pypdf.PdfReader
    budgets = []

    def observed_reader(*args, **kwargs):
        budgets.append(pypdf.get_configuration().zlib_maximum_output_length)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(pypdf, "PdfReader", observed_reader)
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    content = DecodedStreamObject()
    content.set_data(b"q\n" + b" " * 8_100_000 + b"\nQ")
    page[NameObject("/Contents")] = writer._add_object(content.flate_encode())
    path = tmp_path / "expanded.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert path.stat().st_size < 20_000
    result = discover({"root": str(tmp_path), "path": ".", "retain_semantic_samples": True})
    profile = result["assets"][0]["profile"]
    assert 256_000 in budgets
    assert profile["extraction_status"] == "document_stream_limit"
    assert profile["document_signals"]["candidate_terms"] == []


def test_pdf_font_mapping_is_rejected_before_text_expansion():
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    cmap = DecodedStreamObject()
    cmap.set_data(b"1 beginbfchar\n<41> <" + b"0042" * 256 + b">\nendbfchar")
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/ToUnicode"): writer._add_object(cmap),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 0 0 Td (" + b"A" * 240_000 + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    output = BytesIO()
    writer.write(output)
    assert len(output.getvalue()) < 3_000

    tracemalloc.start()
    try:
        lines, status = _document_lines(Path("mapped.pdf"), output.getvalue())
        peak_bytes = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (lines, status) == ([], "document_text_limit")
    assert peak_bytes < 10 * 1024 * 1024


def test_repeated_pdf_form_text_is_counted_before_extraction(monkeypatch):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    cmap = DecodedStreamObject()
    cmap.set_data(b"1 beginbfchar\n<41> <" + b"0042" * 256 + b">\nendbfchar")
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/ToUnicode"): writer._add_object(cmap),
    })
    font_ref = writer._add_object(font)
    form = DecodedStreamObject()
    form.set_data(b"BT /F1 12 Tf 0 0 Td (" + b"A" * 15_000 + b") Tj ET")
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0),
                                          NumberObject(100), NumberObject(100)]),
        NameObject("/Resources"): DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        }),
    })
    form_ref = writer._add_object(form.flate_encode())
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Fm1"): form_ref})
    })
    page_stream = DecodedStreamObject()
    page_stream.set_data(b"/Fm1 Do\n" * 100)
    page[NameObject("/Contents")] = writer._add_object(page_stream.flate_encode())
    output = BytesIO()
    writer.write(output)

    def extraction_must_not_start(*_args, **_kwargs):
        raise AssertionError("PDF extraction started before the Form budget check")

    monkeypatch.setattr(PageObject, "extract_text", extraction_must_not_start)
    assert _document_lines(Path("repeated.pdf"), output.getvalue()) == ([], "document_text_limit")


def test_pdf_aggregate_stream_budget_runs_before_recursive_operand_parsing(monkeypatch):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    xobjects = DictionaryObject()
    for name in ("Fm1", "Fm2"):
        form = DecodedStreamObject()
        form.set_data(b" " * 200_000)
        form.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0),
                                              NumberObject(100), NumberObject(100)]),
        })
        xobjects[NameObject(f"/{name}")] = writer._add_object(form.flate_encode())
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): xobjects})
    content = DecodedStreamObject()
    content.set_data(b"/Fm1 Do /Fm2 Do")
    page[NameObject("/Contents")] = writer._add_object(content.flate_encode())
    output = BytesIO()
    writer.write(output)

    def recursive_parsing_must_not_start(*_args, **_kwargs):
        raise AssertionError("Recursive PDF parsing started before the aggregate stream check")

    monkeypatch.setattr("knowledge_layer.connectors.local_files._pdf_text_operand_bytes",
                        recursive_parsing_must_not_start)
    assert _document_lines(Path("large-forms.pdf"), output.getvalue()) == ([], "document_text_limit")


@pytest.mark.parametrize("text_operation", [
    b"(" + b"A" * 17_000 + b") (x) Tj",
    b"[(" + b"A" * 17_000 + b")] [(x)] TJ",
])
def test_pdf_preflight_counts_all_malformed_text_operands(monkeypatch, text_operation):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b"BT " + text_operation + b" ET")
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    output = BytesIO()
    writer.write(output)

    def extraction_must_not_start(*_args, **_kwargs):
        raise AssertionError("PDF extraction started before all text operands were counted")

    monkeypatch.setattr(PageObject, "extract_text", extraction_must_not_start)
    assert _document_lines(Path("malformed.pdf"), output.getvalue()) == ([], "document_text_limit")


def test_previous_pdf_profile_is_rebuilt_after_parser_policy_change(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf (policy) Tj ET" + b" " * 300_000)
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    path = tmp_path / "policy.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    (tmp_path / "orders.csv").write_text("order_id\nO-1\n", encoding="utf-8")
    config = {"root": str(tmp_path), "path": ".", "retain_semantic_samples": True}
    initial = discover(config)
    old_asset = next(asset for asset in initial["assets"] if asset["name"] == "policy.pdf")
    assert old_asset["profile"]["extraction_status"] == "document_stream_limit"
    old_asset["profile"]["profile_version"] = 4
    old_asset["profile"]["extraction_status"] = "extracted"
    previous = {"assets": initial["assets"], "discovery_complete": True}

    refreshed = discover(config | {"_previous_snapshot": previous})
    assert refreshed["profiling"]["reused_assets"] == ["orders.csv"]
    assert refreshed["profiling"]["reprofiled_assets"] == ["policy.pdf"]
    pdf = next(asset for asset in refreshed["assets"] if asset["name"] == "policy.pdf")
    assert pdf["profile"]["extraction_status"] == "document_stream_limit"


def test_repeated_pdf_forms_stop_at_aggregate_text_budget():
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    form = DecodedStreamObject()
    form.set_data(b"BT /F1 12 Tf 0 0 Td (" + b"A" * 700 + b") Tj ET\n")
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0),
                                          NumberObject(100), NumberObject(100)]),
        NameObject("/Resources"): resources,
    })
    form_ref = writer._add_object(form.flate_encode())
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Fm1"): form_ref}),
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
    })
    page_stream = DecodedStreamObject()
    page_stream.set_data(b"q /Fm1 Do Q\n" * 100)
    page[NameObject("/Contents")] = writer._add_object(page_stream.flate_encode())
    output = BytesIO()
    writer.write(output)
    assert len(output.getvalue()) < 2_000

    lines, status = _document_lines(Path("synthetic.pdf"), output.getvalue())
    assert lines == []
    assert status == "document_text_limit"


@pytest.mark.parametrize("inherited_resources", [False, True])
@pytest.mark.parametrize("subtype", ["/Form", "/PS"])
def test_nested_pdf_forms_stop_before_combined_operation_parsing(inherited_resources, subtype):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    font_ref = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    font_map = DictionaryObject({NameObject("/F1"): font_ref})
    forms = {}
    for label, child in (("C", None), ("B", "C"), ("A", "B")):
        stream = DecodedStreamObject()
        stream.set_data((f"/Fm{child} Do\n".encode() if child else b"") + b"q\n" * 50_000)
        resources = DictionaryObject({
            NameObject("/Font"): font_map,
            NameObject("/XObject"): DictionaryObject({
                NameObject(f"/Fm{child}"): forms[child],
            }) if child else DictionaryObject(),
        })
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): writer._add_object(NameObject(subtype))
            if inherited_resources else NameObject(subtype),
            NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0),
                                              NumberObject(100), NumberObject(100)]),
        })
        if inherited_resources:
            stream[NameObject("/Parent")] = writer._add_object(DictionaryObject({
                NameObject("/Resources"): resources,
            }))
        else:
            stream[NameObject("/Resources")] = resources
        forms[label] = writer._add_object(stream.flate_encode())
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): font_map,
        NameObject("/XObject"): DictionaryObject({NameObject("/FmA"): forms["A"]}),
    })
    page_stream = DecodedStreamObject()
    page_stream.set_data(b"/FmA Do\n")
    page[NameObject("/Contents")] = writer._add_object(page_stream.flate_encode())
    output = BytesIO()
    writer.write(output)
    assert len(output.getvalue()) < 4_000

    lines, status = _document_lines(Path("nested.pdf"), output.getvalue())
    assert lines == []
    assert status == "document_text_limit"
