#!/usr/bin/env python3
"""Convert a guigui_invest analysis report (Markdown) to a styled HTML dashboard.

Supports both report families:

* 当前龟龟策略报告（``## 🎯 投资决策卡``、``## 较上期变化``、``## 执行摘要``、
  ``## 一、定性分析（D1–D6 精要）``、``## 数字溯源汇总``、``## 结构化参数``、
  ``## 分析边界`` 等）；
* 旧版定性报告 ``qualitative_report.md``（``# 定性分析 — 公司 (代码)`` +
  ``## 维度一…`` + 结构化参数表格）。

Usage:
    python3 scripts/report_to_html.py \
        --input output/601088_中国神华/中国神华_601088_分析报告.md \
        --output /tmp/report.html --standalone

Optional:
    --template  Path to Jinja2 HTML template (default: shared/qualitative/templates/dashboard.html)
    --appendix  Path to framework_guide.md (default: shared/qualitative/references/framework_guide.md,
                only rendered for legacy qualitative reports or when passed explicitly)

Security: report bodies contain externally sourced text (WebSearch/PDF). Raw HTML from
Markdown is passed through an allowlist sanitizer and Jinja2 autoescaping is enabled,
so untrusted markup such as ``<script>`` / ``onerror=`` cannot become executable output.
"""

import argparse
import re
import sys
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path

import markdown
from jinja2 import Environment, BaseLoader


# ---------------------------------------------------------------------------
# HTML sanitizer (allowlist) — defense against raw HTML / XSS in untrusted text
# ---------------------------------------------------------------------------

_ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "del", "code", "pre",
    "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "a", "dl", "dt", "dd", "sup", "sub",
})

_VOID_TAGS = frozenset({"br", "hr"})

# Tags whose text content is dropped entirely (including descendants).
_DROP_CONTENT_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed", "svg", "math",
    "template", "noscript", "form", "input", "button", "textarea",
    "select", "option", "link", "meta", "base", "applet", "frame",
    "frameset", "video", "audio", "source", "canvas",
})

# Only these attributes survive, and only on the given tags.
_ALLOWED_ATTRS = {"a": frozenset({"href", "title"})}

_SAFE_SCHEMES = frozenset({"http", "https", "mailto", "tel"})


class _HTMLSanitizer(HTMLParser):
    """Rebuild an HTML fragment from an allowlist, escaping everything else."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._drop_depth = 0

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _safe_href(value):
        value = (value or "").strip()
        if not value:
            return False
        compact = re.sub(r"\s+", "", value).lower()
        match = re.match(r"^([a-z][a-z0-9+.\-]*):", compact)
        if match and match.group(1) not in _SAFE_SCHEMES:
            return False
        return True

    def _rt_attrs(self, tag, attrs):
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            allowed = _ALLOWED_ATTRS.get(tag)
            if not allowed or name not in allowed:
                continue
            if name == "href":
                if not self._safe_href(value):
                    continue
                kept.append(f' href="{_html_escape(value or "", quote=True)}"')
            else:  # title
                kept.append(f' title="{_html_escape(value or "", quote=True)}"')
        return "".join(kept)

    # -- HTMLParser hooks ----------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag not in _ALLOWED_TAGS:
            return
        self._parts.append(f"<{tag}{self._rt_attrs(tag, attrs)}>")

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        self.handle_starttag(tag, attrs)
        if (
            not self._drop_depth
            and tag in _ALLOWED_TAGS
            and tag not in _VOID_TAGS
        ):
            self._parts.append(f"</{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self._drop_depth:
            return
        self._parts.append(_html_escape(data, quote=False))

    def handle_comment(self, data):  # drop comments
        pass

    def handle_decl(self, decl):  # <!DOCTYPE ...>
        pass

    def handle_pi(self, data):  # <? ... ?>
        pass

    def unknown_decl(self, data):
        pass

    def get_html(self):
        return "".join(self._parts)


def sanitize_html(fragment: str) -> str:
    """Return *fragment* with only allowlisted tags/attributes preserved."""
    if not fragment:
        return ""
    parser = _HTMLSanitizer()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:  # pragma: no cover - malformed markup fallback
        return _html_escape(fragment, quote=False)
    return parser.get_html()


# ---------------------------------------------------------------------------
# Markdown → HTML conversion
# ---------------------------------------------------------------------------

def md_to_html(md_text: str) -> str:
    """Convert markdown text to sanitized HTML with tables/fenced code support."""
    if not md_text or not md_text.strip():
        return ""
    raw = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )
    return sanitize_html(raw)


# ---------------------------------------------------------------------------
# Fallback inline CSS for --standalone mode when site CSS not found
# ---------------------------------------------------------------------------

_FALLBACK_CSS = """
:root{--bg:#fafaf7;--bg2:#f0efe9;--bg3:#e8e7e0;--text:#1c1c1a;--text2:#5c5c58;--text3:#8a8a84;--border:rgba(0,0,0,.08);--accent:#1a1a18;--green:#1a7a5a;--green-bg:#e6f4ee;--red:#c0392b;--red-bg:#fceaea;--amber:#a06c1a;--amber-bg:#faf0d8;--blue:#2563a0;--blue-bg:#e8f0fa;--max-width:820px;--padding-x:32px}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--bg2:#1e1e1e;--bg3:#2a2a2a;--text:#e8e8e4;--text2:#a8a8a0;--text3:#6e6e68;--border:rgba(255,255,255,.08);--accent:#e8e8e4;--green:#4ade80;--green-bg:rgba(74,222,128,.1);--red:#f87171;--red-bg:rgba(248,113,113,.1);--amber:#fbbf24;--amber-bg:rgba(251,191,36,.1);--blue:#60a5fa;--blue-bg:rgba(96,165,250,.1)}}
*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Noto Sans SC',system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.7;font-size:15px}
.container{max-width:var(--max-width);margin:0 auto;padding:0 var(--padding-x)}.site-nav{display:flex;align-items:center;justify-content:space-between;padding:16px 0;border-bottom:1px solid var(--border)}.nav-logo{color:var(--text);text-decoration:none;font-weight:600;font-size:15px}.nav-links{display:flex;gap:20px}.nav-links a{color:var(--text3);text-decoration:none;font-size:14px}.nav-links a.active{color:var(--text)}
.report-body{max-width:var(--max-width);margin:0 auto;padding:32px var(--padding-x) 64px;font-size:15px;line-height:1.7;color:var(--text)}
.report-body .header{border-bottom:2px solid var(--accent);padding-bottom:24px;margin-bottom:40px}.report-body .header .ticker{font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--text3);letter-spacing:.5px;text-transform:uppercase}.report-body .header h1{font-size:28px;font-weight:600;margin:6px 0 4px;letter-spacing:-.5px}.report-body .header .date{font-size:13px;color:var(--text3)}
.report-body .verdict{display:flex;align-items:center;gap:12px;margin:24px 0;padding:16px 20px;background:var(--bg2);border-radius:8px;border-left:4px solid var(--green)}.verdict.v-green{border-left-color:var(--green)}.verdict.v-amber{border-left-color:var(--amber)}.verdict.v-red{border-left-color:var(--red)}.verdict-label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text3);font-weight:500}.verdict-text{font-size:15px;font-weight:500}
.report-body h2{font-size:13px;font-weight:500;text-transform:uppercase;letter-spacing:1.5px;color:var(--text3);margin:48px 0 16px;padding-bottom:8px;border-bottom:1px solid var(--border)}.report-body h2:first-of-type{margin-top:0}.report-body h3{font-size:15px;font-weight:600;color:var(--text);margin:24px 0 10px}.report-body h4{font-size:14px;font-weight:500;color:var(--text2);margin:16px 0 8px}
.report-body p{margin-bottom:14px;color:var(--text2)}.report-body p strong{color:var(--text);font-weight:500}.report-body ul,.report-body ol{margin:8px 0 14px 20px;color:var(--text2)}.report-body li{margin:4px 0}.report-body blockquote{border-left:3px solid var(--border);padding:8px 16px;margin:14px 0;color:var(--text3);font-style:italic;font-size:14px}
.report-body .grid{display:grid;gap:10px;margin:16px 0}.report-body .g4{grid-template-columns:1fr 1fr 1fr 1fr}@media(max-width:600px){.report-body .g4{grid-template-columns:1fr 1fr}}.report-body .metric{background:var(--bg2);border-radius:8px;padding:14px 16px}.report-body .metric .label{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}.report-body .metric .value{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:500}.report-body .metric .sub{font-size:12px;color:var(--text3);margin-top:2px}
.report-body .metric.highlight{background:var(--green-bg);border:1px solid rgba(26,122,90,.15)}.report-body .metric.highlight .value{color:var(--green)}.report-body .metric.warn{background:var(--red-bg);border:1px solid rgba(192,57,43,.15)}.report-body .metric.warn .value{color:var(--red)}.report-body .metric.amber-hl{background:var(--amber-bg);border:1px solid rgba(160,108,26,.15)}.report-body .metric.amber-hl .value{color:var(--amber)}
.report-body .tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:4px;font-weight:500}.tag-green{background:var(--green-bg);color:var(--green)}.tag-red{background:var(--red-bg);color:var(--red)}.tag-amber{background:var(--amber-bg);color:var(--amber)}
.badge-strong{background:var(--green-bg);color:var(--green)}.badge-fairly-strong{background:var(--green-bg);color:var(--green)}.badge-medium{background:var(--amber-bg);color:var(--amber)}.badge-weak{background:var(--red-bg);color:var(--red)}
.report-body table{width:100%;border-collapse:collapse;margin:16px 0;font-size:13px}.report-body th{text-align:left;padding:8px 10px;font-weight:400;color:var(--text3);border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:.5px}.report-body th:not(:first-child){text-align:right}.report-body td{padding:8px 10px;border-bottom:1px solid var(--border)}.report-body td:not(:first-child){text-align:right;font-family:'JetBrains Mono',monospace;font-size:13px}.report-body tr:last-child td{border-bottom:none}
.report-body .callout{padding:16px 20px;background:var(--bg2);border-radius:8px;margin:20px 0;font-size:14px;color:var(--text2);line-height:1.7}
.report-body .callout.preamble{border-left:4px solid var(--amber);background:var(--amber-bg)}
.report-body .callout.boundary{border-left:4px solid var(--border)}
.report-body .decision-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:4px 18px 10px;margin:16px 0 24px}
.report-body .decision-card thead{display:none}.report-body .decision-card table{margin:6px 0}.report-body .decision-card td{padding:10px 8px}.report-body .decision-card td:first-child{white-space:nowrap;color:var(--text3);font-family:inherit;font-size:13px}.report-body .decision-card td:not(:first-child){text-align:left;font-family:inherit;font-size:14px;color:var(--text)}
.report-body details{margin:16px 0}.report-body summary{cursor:pointer;font-size:13px;font-weight:500;text-transform:uppercase;letter-spacing:1px;color:var(--text3);padding:10px 0;border-bottom:1px solid var(--border);user-select:none}.report-body details .details-content{padding:16px 0;font-size:14px;color:var(--text2)}
.report-body .footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--border);font-size:12px;color:var(--text3);line-height:1.8}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
@media print{.site-nav{display:none}.report-body{padding:24px;font-size:12px;max-width:100%}}
"""


# ---------------------------------------------------------------------------
# Report parser – splits MD into logical sections
# ---------------------------------------------------------------------------

_RATING_MAP = {
    "强": ("rating-strong", "badge-strong"),
    "较强": ("rating-fairly-strong", "badge-fairly-strong"),
    "中": ("rating-medium", "badge-medium"),
    "中等": ("rating-medium", "badge-medium"),
    "弱": ("rating-weak", "badge-weak"),
    "高可持续": ("rating-strong", "badge-strong"),
    "中等可持续": ("rating-medium", "badge-medium"),
    "低可持续": ("rating-weak", "badge-weak"),
    "优秀": ("rating-strong", "badge-strong"),
    "合格": ("rating-fairly-strong", "badge-fairly-strong"),
    "损害价值": ("rating-weak", "badge-weak"),
    "观察期": ("rating-medium", "badge-medium"),
    "capital-light": ("rating-strong", "badge-strong"),
    "capital-hungry": ("rating-medium", "badge-medium"),
    "存在": ("rating-strong", "badge-strong"),
    "可能存在": ("rating-medium", "badge-medium"),
    "不存在": ("rating-weak", "badge-weak"),
    "正面": ("rating-strong", "badge-strong"),
    "中性": ("rating-medium", "badge-medium"),
    "负面": ("rating-weak", "badge-weak"),
    "低": ("rating-strong", "badge-strong"),
    "高": ("rating-weak", "badge-weak"),
}


def _rating_css(value: str):
    """Return (kpi_card_class, badge_class) for a rating value."""
    for key, classes in _RATING_MAP.items():
        if key in (value or ""):
            return classes
    return ("rating-neutral", "")


# Title / metadata patterns (ordered: turtle report → legacy qualitative → generic)
_TITLE_PATTERNS = (
    ("turtle", re.compile(
        r"^#\s*(?P<name>.+?)\s*[（(](?P<code>[^（()）]+)[）)]\s*龟龟投资策略分析报告",
        re.MULTILINE,
    )),
    ("qualitative", re.compile(
        r"^#\s*定性分析\s*[—\-–]\s*(?P<name>.+?)\s*[（(](?P<code>[^（()）]+)[）)]",
        re.MULTILINE,
    )),
    ("qualitative", re.compile(
        r"^#\s*(?P<name>.+?)\s*[（(](?P<code>[^（()）]+)[）)]\s*[—\-–]",
        re.MULTILINE,
    )),
    ("report", re.compile(
        r"^#\s*(?P<name>.+?)\s*[（(](?P<code>[^（()）]+)[）)]",
        re.MULTILINE,
    )),
)

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_D1_D6_INLINE_RE = re.compile(
    r"\*\*\s*(?P<label>D(?P<num>[1-6]))\s*(?P<name>[^：:*\n]{0,40}?)\s*[：:]\s*"
    r"(?P<summary>[^*]*?)\s*\*\*"
)
_D1_D6_HEADING_RE = re.compile(
    r"^###\s*(?P<title>(?:D[1-6]|维度)[^\n]*)$", re.MULTILINE
)
_TRAILING_RULE_RE = re.compile(r"(?:\n\s*-{3,}\s*)+$")


def _clean_body(body: str) -> str:
    return _TRAILING_RULE_RE.sub("", (body or "")).strip()


def _extract_badge(body: str):
    """Extract a rating badge from a legacy qualitative dimension section."""
    patterns = (
        r"综合评价[：:]\s*\*?\*?(\S+?)\*?\*?(?:\s|$)",
        r"护城河评级[：:]\s*\*?\*?(\S+?)\*?\*?(?:\s|$)",
        r"管理层评价[：:]\s*\*?\*?(\S+?)\*?\*?(?:\s|$)",
        r"MD&A\s*可信度[：:]\s*\*?\*?(\S+?)\*?\*?(?:\s|$)",
        r"资本消耗强度[：:]\s*\*?\*?(\S+?)\*?\*?(?:\s|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            badge = match.group(1).strip("*")
            _, badge_class = _rating_css(badge)
            return badge, badge_class
    return "", ""


def _split_d1_d6(body: str):
    """Parse the "## 一、定性分析（D1–D6 精要）" body into 6 dimension entries.

    Returns (items, intro_html); *items* is empty when the body does not look
    like the current D1–D6 digest layout.
    """
    matches = list(_D1_D6_INLINE_RE.finditer(body))
    if len(matches) >= 3:
        intro = md_to_html(_clean_body(body[: matches[0].start()]))
        items = []
        for idx, match in enumerate(matches):
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
            rest = _clean_body(body[start:end])
            label = match.group("label")
            name = match.group("name").strip()
            summary = match.group("summary").strip()
            title = f"{label} {name}".strip()
            content_md = f"**{summary}**{(' ' + rest) if rest else ''}" if summary else rest
            items.append({
                "title": title,
                "badge": "",
                "badge_class": "",
                "summary": summary,
                "content": md_to_html(content_md),
            })
        return items, intro

    headings = list(_D1_D6_HEADING_RE.finditer(body))
    if len(headings) >= 2:
        intro = md_to_html(_clean_body(body[: headings[0].start()]))
        items = []
        for idx, match in enumerate(headings):
            start = match.end()
            end = headings[idx + 1].start() if idx + 1 < len(headings) else len(body)
            raw = _clean_body(body[start:end])
            title = match.group("title").strip()
            badge, badge_class = _extract_badge(raw)
            items.append({
                "title": title,
                "badge": badge,
                "badge_class": badge_class,
                "summary": "",
                "content": md_to_html(raw),
            })
        return items, intro

    return [], ""


def _section_kind(title: str) -> str:
    """Classify an H2 section title into a rendering kind."""
    if "投资决策卡" in title:
        return "decision"
    if "执行摘要" in title or "Executive Summary" in title:
        return "executive"
    if "较上期变化" in title:
        return "changes"
    if "结构化参数" in title:
        return "parameters"
    if "数字溯源" in title:
        return "traceability"
    if "分析边界" in title:
        return "boundary"
    if "总结与投资启示" in title or "深度总结" in title:
        return "conclusion"
    if "定性分析" in title or "维度" in title or "交叉验证" in title:
        return "dimensions"
    return "other"


def _split_sections(md_text: str):
    """Split markdown at H2 boundaries, preserving document order."""
    matches = list(_H2_RE.finditer(md_text))
    sections = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
        sections.append({"title": match.group(1).strip(), "raw": md_text[start:end]})
    return sections


def _extract_preamble(md_text: str) -> str:
    """Extract meaningful intro text that precedes the first H2 section."""
    first_h2 = _H2_RE.search(md_text)
    intro = md_text[: first_h2.start()] if first_h2 else md_text
    kept = []
    for line in intro.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            continue
        if re.fullmatch(r"-{3,}", stripped):
            continue
        if re.match(r"^\*[^*]*分析日期[^*]*\*$", stripped):
            continue
        kept.append(line)
    return md_to_html("\n".join(kept).strip())


def _pick_heading_text(raw_title: str) -> str:
    """Keep the original heading text; emoji markers are fine in HTML."""
    return re.sub(r"\s+", " ", raw_title or "").strip()


def parse_report(md_text: str) -> dict:
    """Parse a report Markdown document into structured, render-ready fields.

    The returned dict keeps the legacy keys (``executive_summary``,
    ``dimensions``, ``parameters_table`` …) and adds:

    * ``sections`` — ordered H2 sections with ``kind`` / ``content`` /
      ``dimensions`` for template rendering (nothing is silently dropped);
    * ``decision_card`` / ``changes_since_last`` / ``traceability`` /
      ``boundary`` / ``preamble`` for the current turtle report layout;
    * ``report_kind`` / ``is_legacy_qualitative`` / ``parameters_raw``.
    """
    result = {
        "company_name": "",
        "stock_code": "",
        "generated_date": "",
        "report_kind": "report",
        "is_legacy_qualitative": False,
        "preamble": "",
        "decision_card": "",
        "executive_summary": "",
        "changes_since_last": "",
        "dimensions": [],
        "conclusion": "",
        "parameters_table": "",
        "parameters_raw": "",
        "traceability": "",
        "boundary": "",
        "sections": [],
    }

    # --- Title metadata -------------------------------------------------
    for kind, pattern in _TITLE_PATTERNS:
        match = pattern.search(md_text)
        if match:
            name = match.group("name").strip()
            code = match.group("code").strip()
            # Legacy Format A leaves the "—— 商业模式与护城河定性分析" tail on the name
            name = re.sub(r"\s*[—\-–]+\s*(商业模式与护城河)?定性分析.*$", "", name).strip()
            name = re.sub(r"\s*龟龟投资策略分析报告$", "", name).strip()
            result["company_name"] = name
            result["stock_code"] = code
            result["report_kind"] = kind
            break

    # --- Date -----------------------------------------------------------
    date_match = re.search(r"\*生成时间[:：]\s*(.+?)\*", md_text)
    if not date_match:
        date_match = re.search(r"分析日期[：:\s]+(\d{4}-\d{2}-\d{2})", md_text)
    if date_match:
        result["generated_date"] = date_match.group(1).strip().strip("*").strip()

    result["preamble"] = _extract_preamble(md_text)

    # --- H2 sections ----------------------------------------------------
    dim_sections = 0
    legacy_dim_layout = False
    for raw_section in _split_sections(md_text):
        title = _pick_heading_text(raw_section["title"])
        body = raw_section["raw"]
        kind = _section_kind(title)
        section = {
            "title": title,
            "kind": kind,
            "content": "",
            "badge": "",
            "badge_class": "",
            "dimensions": [],
        }

        if kind == "decision":
            section["content"] = md_to_html(_clean_body(body))
            result["decision_card"] = section["content"]
        elif kind == "executive":
            section["content"] = md_to_html(_clean_body(body))
            result["executive_summary"] = section["content"]
        elif kind == "changes":
            section["content"] = md_to_html(_clean_body(body))
            result["changes_since_last"] = section["content"]
        elif kind == "parameters":
            section["content"] = md_to_html(_clean_body(body))
            result["parameters_table"] = section["content"]
            result["parameters_raw"] += _clean_body(body) + "\n"
        elif kind == "traceability":
            section["content"] = md_to_html(_clean_body(body))
            result["traceability"] = section["content"]
        elif kind == "boundary":
            section["content"] = md_to_html(_clean_body(body))
            result["boundary"] = section["content"]
        elif kind == "conclusion":
            section["content"] = md_to_html(_clean_body(body))
            result["conclusion"] = section["content"]
        elif kind == "dimensions":
            dim_sections += 1
            dims, intro = _split_d1_d6(_clean_body(body))
            if dims:
                section["dimensions"] = dims
                section["content"] = intro
            else:
                # Legacy qualitative layout: one H2 dimension section with a badge
                legacy_dim_layout = True
                cleaned = _clean_body(body)
                badge, badge_class = _extract_badge(cleaned)
                section["badge"] = badge
                section["badge_class"] = badge_class
                section["dimensions"] = [{
                    "title": "",
                    "badge": badge,
                    "badge_class": badge_class,
                    "summary": "",
                    "content": md_to_html(cleaned),
                }]
            result["dimensions"].extend(section["dimensions"])
        else:
            section["content"] = md_to_html(_clean_body(body))

        result["sections"].append(section)

    # Decision card always comes first (first screen requirement)
    result["sections"].sort(key=lambda sec: 0 if sec["kind"] == "decision" else 1)

    # A qualitative report keeps the legacy treatment even when the modern
    # 投资决策卡 is present (BA reports are required to carry one).
    result["is_legacy_qualitative"] = (
        result["report_kind"] == "qualitative"
        or legacy_dim_layout
        or (not result["decision_card"] and dim_sections > 0)
    )

    return result


# ---------------------------------------------------------------------------
# KPI cards / verdict / data pack helpers
# ---------------------------------------------------------------------------

def _find_table_param(md_text: str, name: str) -> str:
    pattern = rf"\|\s*{re.escape(name)}\s*\|\s*(.+?)\s*\|"
    match = re.search(pattern, md_text)
    return match.group(1).strip() if match else ""


def _find_code_param(block: str, name: str) -> str:
    pattern = rf"^\s*{re.escape(name)}\s*:\s*([^\s#]+)"
    match = re.search(pattern, block or "", re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip().strip('"').strip("'")
    if value.lower() in ("null", "none", "n/a", "na", "~"):
        return ""
    return value


def extract_kpi_cards(md_text: str) -> list:
    """Extract KPI values from the structured parameters table (legacy layout)."""
    cards = []

    def _to_card_css(value: str) -> str:
        positive = ["强", "优秀", "存在", "高可持续", "正面", "capital-light"]
        negative = ["弱", "损害价值", "不存在", "低可持续", "负面"]
        neutral = ["中", "中等", "合格", "观察期", "中等可持续", "capital-hungry", "可能存在"]
        for item in positive:
            if item in value:
                return "highlight"
        for item in negative:
            if item in value:
                return "warn"
        for item in neutral:
            if item in value:
                return "amber-hl"
        return ""

    roe = _find_table_param(md_text, "roe_5y_avg")
    if roe:
        try:
            roe_val = float(re.search(r"[\d.]+", roe).group())
            css = "highlight" if roe_val >= 15 else ("amber-hl" if roe_val >= 8 else "warn")
        except (ValueError, AttributeError):
            css = ""
        cards.append({"label": "5Y Avg ROE", "value": roe, "css_class": css, "sub": ""})

    for param, label in (
        ("moat_rating", "护城河评级"),
        ("moat_sustainability", "可持续性"),
        ("management_rating", "管理层评价"),
        ("capital_intensity", "资本强度"),
        ("entry_barrier", "进入壁垒"),
        ("moat_existence", "优势存在性"),
    ):
        value = _find_table_param(md_text, param)
        if value:
            cards.append({
                "label": label,
                "value": value,
                "css_class": _to_card_css(value),
                "sub": "",
            })

    cyclicality = _find_table_param(md_text, "cyclicality")
    if cyclicality:
        cycle_position = _find_table_param(md_text, "cycle_position")
        cards.append({
            "label": "周期性",
            "value": cyclicality,
            "css_class": "",
            "sub": cycle_position,
        })

    return cards


def extract_data_pack_info(dp_text: str) -> dict:
    """Extract header-level info from data_pack_market.md."""
    info = {
        "current_price": "",
        "market_cap": "",
        "exchange": "",
        "industry": "",
    }

    price_m = re.search(r"当前价格[|\s]+(\d+\.?\d*)", dp_text)
    if price_m:
        info["current_price"] = price_m.group(1)

    mcap_m = re.search(r"总市值\s*\(万元\)\s*\|\s*([\d,.]+)", dp_text)
    if mcap_m:
        val = mcap_m.group(1).replace(",", "")
        try:
            value = float(val)
            info["market_cap"] = f"{value / 10000:.0f}亿"
        except ValueError:
            info["market_cap"] = mcap_m.group(1)

    exchange_m = re.search(r"交易所\s*\|\s*(\S+)", dp_text)
    if exchange_m:
        info["exchange"] = exchange_m.group(1)

    industry_m = re.search(r"行业\s*\|\s*(\S+)", dp_text)
    if industry_m:
        info["industry"] = industry_m.group(1)

    return info


def _extract_report_price(md_text: str) -> str:
    """Fallback: read the last close price from the report body when no data pack."""
    match = re.search(
        r"(?:最近已获取收盘价|最新收盘价|当前价格)[^\n|]*\|\s*(?:¥|￥)?\s*([\d]+(?:\.\d+)?)\s*元?",
        md_text,
    )
    return match.group(1) if match else ""


def build_verdict(md_text: str, report: dict = None) -> dict:
    """Build the verdict banner (decision status for turtle, moat for legacy)."""
    report = report or {}
    params_raw = report.get("parameters_raw", "")

    decision_status = _find_code_param(params_raw, "decision_status")
    audit_status = _find_code_param(params_raw, "audit_status")

    verdict_text = ""
    final_m = re.search(
        r"(?:一句话最终结论|一句话结论)[：:]\s*\*{0,2}([^\n*]+)",
        md_text,
        re.MULTILINE,
    )
    if final_m:
        verdict_text = final_m.group(1).strip().strip("*").strip()

    if decision_status or audit_status:
        status = (decision_status or "unverified").strip()
        status_lower = status.lower()
        if status_lower in ("verified", "pass", "passed", "通过", "已核实"):
            tag_class, verdict_class, tag_text = "tag-green", "v-green", "VERIFIED"
            verdict_text = verdict_text or f"决策状态：{status}"
        else:
            tag_class, verdict_class, tag_text = "tag-amber", "v-amber", "UNVERIFIED"
            verdict_text = verdict_text or f"决策状态：{status}"
            if audit_status and audit_status.upper() not in ("COMPLETED", "PASS"):
                tag_text = "待核实 · 审计未完成"
        return {
            "verdict_class": verdict_class,
            "verdict_tag_class": tag_class,
            "verdict_tag": tag_text,
            "verdict_text": verdict_text,
        }

    # Legacy qualitative report: derive status from moat rating parameters
    moat = _find_table_param(md_text, "moat_rating")
    sust = _find_table_param(md_text, "moat_sustainability")

    tag_map = {
        "强": ("tag-green", "v-green", "STRONG MOAT"),
        "较强": ("tag-green", "v-green", "FAIRLY STRONG"),
        "中": ("tag-amber", "v-amber", "MODERATE"),
        "弱": ("tag-red", "v-red", "WEAK"),
    }
    tag_class, verdict_class, tag_text = tag_map.get(
        moat, ("tag-amber", "v-amber", moat.upper() if moat else "N/A")
    )

    if not verdict_text:
        verdict_text = f"护城河评级 {moat}，可持续性 {sust}" if moat else ""

    return {
        "verdict_class": verdict_class,
        "verdict_tag_class": tag_class,
        "verdict_tag": tag_text,
        "verdict_text": verdict_text,
    }


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "shared" / "qualitative" / "templates" / "dashboard.html"
DEFAULT_APPENDIX = Path(__file__).resolve().parent.parent / "shared" / "qualitative" / "references" / "framework_guide.md"


def _build_slug(report: dict) -> str:
    if not report.get("stock_code"):
        return ""
    code = (
        report["stock_code"]
        .replace(".SH", "").replace(".SZ", "").replace(".HK", "").replace(".US", "")
    )
    name = report.get("company_name") or ""
    suffix = "turtle" if report.get("report_kind") == "turtle" else "qualitative"
    return f"{name}-{code}-{suffix}".lower().replace(" ", "-")


def build_html(
    md_text: str,
    template_text: str,
    *,
    data_pack_text: str = "",
    appendix_md: str = "",
    standalone_css: str = "",
    force_appendix: bool = False,
):
    """Render a report to HTML. Returns ``(html, stats)``.

    Jinja2 autoescaping is always enabled; only sanitized Markdown fragments are
    injected with ``|safe`` in the template.
    """
    report = parse_report(md_text)
    kpi_cards = extract_kpi_cards(md_text)
    verdict = build_verdict(md_text, report)

    dp_info = {"current_price": "", "market_cap": "", "exchange": "", "industry": ""}
    if data_pack_text:
        dp_info = extract_data_pack_info(data_pack_text)
    if not dp_info["current_price"]:
        dp_info["current_price"] = _extract_report_price(md_text)

    is_legacy = report["is_legacy_qualitative"]
    appendix_html = ""
    if appendix_md and (force_appendix or is_legacy):
        appendix_html = md_to_html(appendix_md)

    report_kind = report["report_kind"]
    if report_kind == "turtle":
        report_label = "龟龟投资策略分析"
    elif is_legacy:
        report_label = "商业质量评估"
    else:
        report_label = "投资分析"

    env = Environment(loader=BaseLoader(), autoescape=True)
    template = env.from_string(template_text)
    html = template.render(
        company_name=report["company_name"],
        stock_code=report["stock_code"],
        generated_date=report["generated_date"],
        report_kind=report_kind,
        report_label=report_label,
        is_legacy_qualitative=is_legacy,
        preamble=report["preamble"],
        sections=report["sections"],
        dimensions=report["dimensions"],
        current_price=dp_info["current_price"],
        market_cap=dp_info["market_cap"],
        exchange=dp_info["exchange"],
        industry=dp_info["industry"],
        slug=_build_slug(report),
        standalone_css=standalone_css,
        kpi_cards=kpi_cards,
        framework_guide=appendix_html,
        **verdict,
    )

    stats = {
        "report_kind": report_kind,
        "is_legacy_qualitative": is_legacy,
        "sections": len(report["sections"]),
        "dimensions": len(report["dimensions"]),
        "has_decision_card": bool(report["decision_card"]),
        "has_executive_summary": bool(report["executive_summary"]),
        "has_changes": bool(report["changes_since_last"]),
        "has_traceability": bool(report["traceability"]),
        "has_parameters": bool(report["parameters_table"]),
        "has_boundary": bool(report["boundary"]),
        "has_appendix": bool(appendix_html),
        "kpi_cards": len(kpi_cards),
    }
    return html, stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert an analysis report MD to an HTML dashboard"
    )
    parser.add_argument("--input", required=True, help="Path to the report .md file")
    parser.add_argument("--output", required=True, help="Output HTML path")
    parser.add_argument(
        "--template",
        default=None,
        help="Jinja2 template path (default: shared/qualitative/templates/dashboard.html)",
    )
    parser.add_argument(
        "--appendix",
        default=None,
        help="Path to framework_guide.md (default: auto-detect; only rendered for "
             "legacy qualitative reports unless explicitly passed)",
    )
    parser.add_argument(
        "--data-pack",
        default=None,
        help="Path to data_pack_market.md for header stats extraction",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Embed CSS inline for local viewing (no external CSS dependencies)",
    )
    args = parser.parse_args(argv)

    # --- Resolve paths ---
    template_path = Path(args.template) if args.template else DEFAULT_TEMPLATE
    appendix_path = Path(args.appendix) if args.appendix else DEFAULT_APPENDIX
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    if not template_path.exists():
        print(f"Error: Template not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    # --- Read inputs ---
    md_text = input_path.read_text(encoding="utf-8")
    template_text = template_path.read_text(encoding="utf-8")

    appendix_md = ""
    if appendix_path.exists():
        try:
            appendix_md = appendix_path.read_text(encoding="utf-8")
        except OSError:
            appendix_md = ""

    data_pack_text = ""
    data_pack_path = (
        Path(args.data_pack) if args.data_pack
        else input_path.parent / "data_pack_market.md"
    )
    if data_pack_path.exists():
        try:
            data_pack_text = data_pack_path.read_text(encoding="utf-8")
        except OSError:
            data_pack_text = ""

    # --- Load standalone CSS if requested ---
    standalone_css = ""
    if args.standalone:
        site_root = Path.home() / "Projects" / "Teracnejiang.com"
        css_parts = []
        for css_file in ["assets/css/style.css", "assets/css/report.css"]:
            css_path = site_root / css_file
            if css_path.exists():
                css_parts.append(css_path.read_text(encoding="utf-8"))
        if css_parts:
            standalone_css = "\n".join(css_parts)
        else:
            standalone_css = _FALLBACK_CSS

    # --- Render ---
    html, stats = build_html(
        md_text,
        template_text,
        data_pack_text=data_pack_text,
        appendix_md=appendix_md,
        standalone_css=standalone_css,
        force_appendix=bool(args.appendix),
    )

    # --- Write output ---
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    print(f"HTML report generated: {output_path}")
    print(f"  Report kind: {stats['report_kind']}"
          f"{' (legacy qualitative compatible)' if stats['is_legacy_qualitative'] else ''}")
    print(f"  Sections: {stats['sections']} · Dimensions: {stats['dimensions']}")
    print(f"  Decision card: {'yes' if stats['has_decision_card'] else 'no'}"
          f" · Executive summary: {'yes' if stats['has_executive_summary'] else 'no'}"
          f" · Changes: {'yes' if stats['has_changes'] else 'no'}")
    print(f"  Traceability: {'yes' if stats['has_traceability'] else 'no'}"
          f" · Parameters: {'yes' if stats['has_parameters'] else 'no'}"
          f" · Boundary: {'yes' if stats['has_boundary'] else 'no'}")
    print(f"  KPI cards: {stats['kpi_cards']} · Appendix: {'yes' if stats['has_appendix'] else 'no'}")


if __name__ == "__main__":
    main()
