"""Tests for pdf_preprocessor text exports (GLM text-first mode)."""

import json
import os

import pytest

from pdf_preprocessor import write_text_exports, SECTION_NAMES


@pytest.fixture
def contexts():
    return {
        "P2": "受限资产内容：受限货币资金...",
        "P3": None,
        "P4": "关联交易：关联方销售 ...",
        "P6": None,
        "P13": "非经常性损益：政府补助...",
        "MDA": "管理层讨论与分析：报告期营收增长...",
        "SUB": None,
    }


@pytest.fixture
def pages():
    return [(1, "目录"), (2, "正文" * 200), (3, "更多正文" * 300)]


@pytest.fixture
def meta():
    return {"pdf_file": "test.pdf", "extract_time": "2026-10-30 00:00:00",
            "total_pages": 3, "sections_found": 3, "sections_total": 7}


def test_sections_digest(tmp_path, pages, contexts, meta):
    out = str(tmp_path / "sections.md")
    files = write_text_exports(pages, contexts, meta, out, full_text=False)
    assert files == [out]
    content = open(out, encoding="utf-8").read()
    assert "年报章节提取" in content
    for sid, name in SECTION_NAMES.items():
        assert f"## {sid} · {name}" in content
    assert "受限货币资金" in content
    assert "未在 PDF 中定位到" in content   # P3 placeholder


def test_full_text_parts(tmp_path, pages, contexts, meta):
    out = str(tmp_path / "sections.md")
    files = write_text_exports(pages, contexts, meta, out,
                               full_text=True, full_text_part_chars=1000)
    assert len(files) > 1  # 300*3+200*2 > 1000 chars → multiple parts
    part1 = open(files[1], encoding="utf-8").read()
    assert "年报全文分片 1/" in part1
    assert "--- p.1 ---" in part1
    assert "目录" in part1


def test_empty_pdf_pages(tmp_path, contexts, meta):
    out = str(tmp_path / "sections.md")
    with pytest.raises(RuntimeError, match="OCR"):
        write_text_exports([(1, "")], contexts, meta, out, full_text=True)
    assert not os.path.exists(out)


def test_json_output_compatible(tmp_path, contexts, meta):
    """JSON structure stays compatible with the original schema."""
    output = {
        "metadata": meta,
        **{sid: contexts.get(sid) for sid in
           ["P2", "P3", "P4", "P6", "P13", "MDA", "SUB"]},
    }
    path = tmp_path / "pdf_sections.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["metadata"]["sections_total"] == 7
    assert loaded["P2"].startswith("受限资产")
    assert loaded["P3"] is None


# ---------------------------------------------------------------------------
# Extraction robustness / provenance
# ---------------------------------------------------------------------------

def test_zone_detection_ignores_cross_reference():
    """A financial-report page quoting '详见第三节管理层讨论与分析' is not MDA."""
    from pdf_preprocessor import detect_zones

    pages = [
        (1, "第八节 财务报告\n公司财务报表如下"),
        (2, "详见第三节 管理层讨论与分析 的相关说明\n货币资金 100"),
        (3, "合并资产负债表\n单位：元"),
    ]
    zones = detect_zones(pages)
    assert zones[1] == "FIN_ZONE"
    assert zones[2] == "FIN_ZONE"  # inherits FIN_ZONE, not MDA
    assert zones[3] == "FIN_ZONE"


def test_zone_detection_real_heading():
    from pdf_preprocessor import detect_zones

    pages = [
        (1, "第一节 重要提示、目录和释义\n本报告..."),
        (2, "第三节 管理层讨论与分析\n经营情况讨论..."),
    ]
    zones = detect_zones(pages)
    assert zones[1] == "INTRO_ZONE"
    assert zones[2] == "MDA_ZONE"


def test_chunk_header_does_not_overstate_coverage(tmp_path, contexts, meta):
    """Non-contiguous pages must not be labelled as one continuous range."""
    output = str(tmp_path / "sections.md")
    pages = [(1, "甲" * 10), (2, ""), (3, "乙" * 10)]
    files = write_text_exports(pages, contexts, meta, output,
                               full_text=True, full_text_part_chars=100)
    html = open(files[1], encoding="utf-8").read()
    assert "非连续" in html
    manifest = json.loads(open(str(tmp_path / "sections_manifest.json"),
                              encoding="utf-8").read())
    assert manifest["parts"][0]["pages"] == [1, 3]
    assert manifest["empty_pages"] == [2]


def test_manifest_and_digest_carry_extraction_provenance(tmp_path, contexts, meta):
    meta = dict(meta)
    meta["extraction"] = {
        "extraction_method": "pymupdf_fallback",
        "page_errors": {"2": "extract_tables: boom"},
        "ocr_candidate_pages": [5],
        "empty_pages": [5],
        "garbled_pages": [],
    }
    output = str(tmp_path / "sections.md")
    files = write_text_exports([(1, "正文"), (5, "")], contexts, meta, output,
                               full_text=True, full_text_part_chars=1000)
    digest = open(files[0], encoding="utf-8").read()
    assert "提取方式: pymupdf_fallback" in digest
    assert "需OCR/外部补充 1 页" in digest
    manifest = json.loads(open(str(tmp_path / "sections_manifest.json"),
                              encoding="utf-8").read())
    assert manifest["extraction"]["extraction_method"] == "pymupdf_fallback"
    assert manifest["extraction"]["ocr_candidate_pages"] == [5]


class _FakePage:
    def __init__(self, text, fail_tables=False, fail_text=False):
        self._text = text
        self._fail_tables = fail_tables
        self._fail_text = fail_text

    def extract_text(self):
        if self._fail_text:
            raise ValueError("text boom")
        return self._text

    def extract_tables(self):
        if self._fail_tables:
            raise ValueError("table boom")
        return []


class _FakePDF:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_extract_all_pages_isolates_page_errors(tmp_path, monkeypatch):
    """One failing page must not turn the whole PDF into 'Cannot open PDF'."""
    import pdf_preprocessor as pp

    pdf_path = tmp_path / "fake.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    fake = _FakePDF([
        _FakePage("第一页正文"),
        _FakePage("第二页正文", fail_tables=True),
        _FakePage("", fail_text=True),
        _FakePage("第四页正文"),
    ])
    monkeypatch.setattr(pp.pdfplumber, "open", lambda path: fake)

    integrity = {}
    pages = pp.extract_all_pages(str(pdf_path), meta=integrity)
    assert [text for _, text in pages] == [
        "第一页正文", "第二页正文", "", "第四页正文"]
    assert "2" in integrity["page_errors"]   # table failure recorded, text kept
    assert "3" in integrity["page_errors"]   # text failure recorded
    assert integrity["empty_pages"] == [3]
    assert 3 in integrity["ocr_candidate_pages"]
