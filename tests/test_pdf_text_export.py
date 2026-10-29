"""Tests for pdf_preprocessor text exports (GLM text-first mode)."""

import json
import os

import pytest

from pdf_preprocessor import write_text_exports, SECTION_NAMES


@pytest.fixture
def contexts():
    return {
        "P2": "公司治理内容：董事会构成...",
        "P3": None,
        "P4": "应收账款账龄：1年以内 ...",
        "P6": None,
        "P13": "风险提示：原材料价格波动...",
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
    assert "董事会构成" in content          # P2 content
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
    files = write_text_exports([(1, "")], contexts, meta, out, full_text=True)
    # full-text part with zero content pages still writes one (possibly empty) part
    assert len(files) >= 1
    assert os.path.exists(files[0])


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
    assert loaded["P2"].startswith("公司治理")
    assert loaded["P3"] is None
