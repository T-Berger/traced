"""Draft review PDF with saved case records and export-time integrity hashes."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF, XPos, YPos
from fpdf.fonts import FontFace

from .analysis import EVIDENCE_TYPES, READY, Assessment, assess, is_supported_platform
from .evidence import PREPARER_STATEMENT, REVIEW_NOTE, evidence_item
from .settings import CATEGORY_LABELS, AlertSettings

PRIMARY = (3, 128, 149)
PRIMARY_LIGHT = (230, 243, 245)
YELLOW = (251, 216, 132)
YELLOW_LIGHT = (254, 247, 230)
INK = (19, 18, 18)
MUTED = (94, 90, 87)
LINE = (222, 217, 213)
RED = (200, 30, 30)
SOFT = (246, 244, 242)
OK = (10, 122, 90)

_FONT_DIRS = [
    os.environ.get("TRACED_PDF_FONT_DIR", ""),
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    "/usr/share/fonts/truetype/msttcorefonts",
    "/usr/share/fonts/truetype/dejavu",
]
_MONO = Path(__file__).resolve().parents[1] / "review/assets/traced/fonts/IBMPlexMono-Regular.ttf"


def _find(*names: str) -> Path | None:
    for d in filter(None, _FONT_DIRS):
        for n in names:
            p = Path(d) / n
            if p.is_file():
                return p
    return None


def _utc(ts) -> str:
    if not ts:
        return "Not recorded"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class EvidencePDF(FPDF):
    def __init__(self, report_id: str, subject: str, manifest_hash: str):
        super().__init__(orientation="P", unit="mm", format="Letter")
        self.report_id, self.subject, self.manifest_hash = report_id, subject, manifest_hash
        self.set_margins(20, 22, 20)
        self.set_auto_page_break(True, margin=22)
        self.unicode = self._load_fonts()
        self.replaced_glyphs = False

    def _load_fonts(self) -> bool:
        regular = _find("Trebuchet MS.ttf", "trebuc.ttf", "DejaVuSans.ttf")
        bold = _find("Trebuchet MS Bold.ttf", "trebucbd.ttf", "DejaVuSans-Bold.ttf")
        italic = _find("Trebuchet MS Italic.ttf", "trebucit.ttf", "DejaVuSans-Oblique.ttf")
        if not regular:
            return False
        self.add_font("body", "", regular)
        self.add_font("body", "B", bold or regular)
        self.add_font("body", "I", italic or regular)
        fallbacks = []
        wide = _find("Arial Unicode.ttf", "DejaVuSans.ttf")
        if wide:
            self.add_font("wide", "", wide)
            self.add_font("wide", "B", wide)
            self.add_font("wide", "I", wide)
            fallbacks.append("wide")
        if _MONO.is_file():
            self.add_font("mono", "", _MONO)
        if fallbacks:
            self.set_fallback_fonts(fallbacks, exact_match=False)
        return True

    # ----------------------------------------------------------------- text helpers

    def clean(self, text: Any) -> str:
        text = "" if text is None else str(text)
        if not self.unicode:
            safe = text.encode("latin-1", "replace").decode("latin-1")
            self.replaced_glyphs |= safe != text
            return safe
        # Emoji and other astral-plane symbols have no printable glyph in the embedded fonts.
        out = "".join(ch if ord(ch) <= 0xFFFF else "□" for ch in text)
        self.replaced_glyphs |= out != text
        return out

    def font(self, size: float = 10, style: str = "", mono: bool = False):
        family = "body" if self.unicode else "helvetica"
        if mono:
            family = "mono" if self.unicode and _MONO.is_file() else "courier"
            style = ""
        self.set_font(family, style, size)

    def para(self, text: str, size=10, style="", color=INK, h=5.2, align="L"):
        self.font(size, style)
        self.set_text_color(*color)
        self.multi_cell(0, h, self.clean(text), align=align, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def heading(self, text: str, level: int = 1):
        if level == 1:
            if self.get_y() > self.h - 70:
                self.add_page()
            self.ln(4)
            self.start_section(text, level=0)
            self.font(15, "B")
            self.set_text_color(*INK)
            self.cell(0, 9, self.clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_draw_color(*PRIMARY)
            self.set_line_width(0.8)
            self.line(self.l_margin, self.get_y(), self.l_margin + 18, self.get_y())
            self.ln(4)
        else:
            if self.get_y() > self.h - 50:
                self.add_page()
            self.ln(2)
            self.font(11, "B")
            self.set_text_color(*INK)
            self.cell(0, 7, self.clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def box(self, text: str, fill=SOFT, bar=PRIMARY, size=10, style=""):
        """Shaded paragraph with a coloured left bar."""
        self.font(size, style)
        x, w = self.l_margin, self.w - self.l_margin - self.r_margin
        lines = self.multi_cell(
            w - 8, 5.2, self.clean(text), align="L", dry_run=True, output="LINES"
        )
        h = len(lines) * 5.2 + 6
        if self.get_y() + h > self.h - self.b_margin:
            self.add_page()
        y = self.get_y()
        self.set_fill_color(*fill)
        self.rect(x, y, w, h, style="F")
        self.set_fill_color(*bar)
        self.rect(x, y, 1.4, h, style="F")
        self.set_xy(x + 5, y + 3)
        self.set_text_color(*INK)
        self.multi_cell(
            w - 8, 5.2, self.clean(text), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        self.set_y(y + h + 3)

    def kv(
        self, rows: list[tuple[str, Any]], widths=(48, 128), links: dict[int, str] | None = None
    ):
        """Two-column key/value table."""
        links = links or {}
        self.set_fill_color(255, 255, 255)
        self.font(9.5)
        self.set_text_color(*INK)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        with self.table(
            col_widths=widths,
            first_row_as_headings=False,
            borders_layout="HORIZONTAL_LINES",
            line_height=5,
            padding=(1.6, 2),
            text_align=("LEFT", "LEFT"),
            cell_fill_mode="NONE",
        ) as table:
            for i, (k, v) in enumerate(rows):
                row = table.row()
                row.cell(self.clean(k), style=FontFace(emphasis="BOLD", color=MUTED))
                row.cell(self.clean(v), link=links.get(i))
        self.ln(3)

    def grid(self, headings: list[str], rows: list[list[Any]], widths, links=None):
        links = links or {}
        self.set_fill_color(255, 255, 255)
        self.font(9)
        self.set_text_color(*INK)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        with self.table(
            col_widths=widths,
            headings_style=FontFace(emphasis="BOLD", color=INK, fill_color=SOFT),
            borders_layout="HORIZONTAL_LINES",
            line_height=4.8,
            padding=(1.6, 2),
            text_align="LEFT",
            cell_fill_mode="NONE",
        ) as table:
            head = table.row()
            for h in headings:
                head.cell(self.clean(h))
            for r_i, r in enumerate(rows):
                row = table.row()
                for c_i, value in enumerate(r):
                    row.cell(self.clean(value), link=links.get((r_i, c_i)))
        self.ln(3)

    def pill(self, text: str, fill, color=(255, 255, 255)):
        self.font(8, "B")
        w = self.get_string_width(text.upper()) + 6
        self.set_fill_color(*fill)
        self.set_text_color(*color)
        self.cell(w, 5.5, text.upper(), fill=True, align="C")
        self.set_text_color(*INK)

    # ----------------------------------------------------------------- page furniture

    def header(self):
        if self.page_no() == 1:
            return
        self.set_fill_color(*PRIMARY)
        self.rect(0, 0, self.w, 3, style="F")
        self.set_y(9)
        self.font(8)
        self.set_text_color(*MUTED)
        self.cell(0, 4, self.clean(f"Draft evidence for review · {self.subject}"))
        self.set_x(self.l_margin)
        self.cell(0, 4, self.report_id, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(self.t_margin)

    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)
        self.font(7.5)
        self.set_text_color(*MUTED)
        self.cell(
            0,
            4,
            f"Manifest SHA-256 {self.manifest_hash[:16]}…  ·  Draft for human review",
        )
        self.set_x(self.l_margin)
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")


# --------------------------------------------------------------------------- sections


def _number(value):
    return f"{value:,}" if value is not None else "Unknown"


def _known_views(items):
    known = [item["reach"]["views"] for item in items if item["reach"]["views"] is not None]
    unknown = sum(item["reach"]["views"] is None for item in items)
    if not known:
        return f"Recorded views unknown; {unknown} posts unknown"
    return f"{_number(sum(known))} known recorded views; {unknown} posts unknown"


def _cover(pdf: EvidencePDF, case, items, accounts, settings, generated, manifest_hash):
    pdf.add_page()
    pdf.set_fill_color(*PRIMARY)
    pdf.rect(0, 0, pdf.w, 92, style="F")
    pdf.set_fill_color(*YELLOW)
    pdf.rect(0, 92, pdf.w, 2.2, style="F")
    pdf.set_xy(20, 24)
    pdf.pill("Draft evidence for review", YELLOW, INK)
    pdf.set_xy(20, 36)
    pdf.font(24, "B")
    pdf.set_text_color(255, 255, 255)
    pdf.multi_cell(
        170,
        10,
        pdf.clean(f"Draft evidence candidates about {case.get('subject_name')}"),
        align="L",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_x(20)
    pdf.font(11)
    pdf.multi_cell(
        170,
        6,
        pdf.clean(f"{case.get('subject_role') or ''}\nPlatform: X (formerly Twitter)"),
        align="L",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_y(104)

    posted = [
        datetime.fromisoformat(i["content"]["posted_at"])
        for i in items
        if i["content"]["posted_at"]
    ]
    period = f"{min(posted):%Y-%m-%d} to {max(posted):%Y-%m-%d}" if posted else "Not recorded"
    prepared = settings.notifier_name or "Not yet entered (see Section 7)"
    if settings.notifier_organisation:
        prepared += f", {settings.notifier_organisation}"
    pdf.kv(
        [
            ("Report ID", pdf.report_id),
            ("Generated", _utc(generated)),
            ("Prepared by", prepared),
            ("Subject", case.get("subject_name")),
            ("Evidence items", f"{len(items)} model candidates"),
            ("Pattern evidence", f"{len(accounts)} accounts with grouped candidates"),
            ("Posting period", period),
            ("Known views", _known_views(items)),
            ("Manifest SHA-256", manifest_hash),
        ]
    )
    pdf.box(
        "This is a draft of model-labeled posts and case-scoped saved records for human review. "
        "No human decision or policy violation is established. Extracted text and source mapping "
        "require independent verification. Attached saved bytes are hashed at export."
    )
    pdf.para(
        "Sections: 1 Summary · 2 Evidence types · 3 Collection metadata and saved records · "
        "4 Evidence items · 5 Pattern evidence · 6 Integrity and verification · 7 Preparation "
        "and review. Saved raw records and the evidence manifest are attached.",
        size=9,
        color=MUTED,
    )


def _summary(pdf: EvidencePDF, items, accounts):
    pdf.add_page()
    pdf.heading("1. Summary")
    rows, links = [], {}
    for i, it in enumerate(items):
        rows.append(
            [
                f"E-{i + 1}",
                f"@{it['attribution']['handle']}",
                f"{it['classification']['category']}\n{it['classification']['platform_rule']}",
                _utc(it["content"]["posted_at"])[:16],
                _number(it["reach"]["views"]),
                "Open post",
            ]
        )
        links[(i, 5)] = it["content"]["url"]
    pdf.grid(
        ["Item", "Account", "Category and X rule", "Posted (UTC)", "Views", "Link"],
        rows,
        (16, 34, 56, 32, 16, 22),
        links,
    )
    pdf.heading("Human review required", 2)
    pdf.para(
        "The model labels, source mapping, context and relevant platform rules require independent "
        "human review. No reporting or removal decision is recorded in this draft."
    )


def _evidence_types(pdf: EvidencePDF):
    pdf.heading("2. Evidence types")
    pdf.para(
        "Each evidence item in Section 4 is organised by the following types of evidence.",
        color=MUTED,
    )
    pdf.ln(1)
    pdf.grid(["Evidence type", "What it shows"], [list(t) for t in EVIDENCE_TYPES], (46, 130))


def _method(pdf: EvidencePDF, items, provenance, generated):
    pdf.heading("3. Collection metadata and saved records")
    pdf.para(
        "Collection metadata and saved native records are read from the selected analytics case "
        "in one read snapshot. SHA-256 hashes identify the attached bytes at export; "
        "they do not prove collection-time integrity, authenticity or text extraction."
    )
    pdf.ln(1)
    pdf.para(
        "Classification scores and rationales are model proposals. Their labels and linked "
        "records do not establish factual truth, violations, independent corroboration or a "
        "human review decision. Verify originals, context, attribution and rules."
    )
    pdf.ln(2)
    pdf.heading("Recorded collection metadata", 2)
    runs: dict[str, dict] = {}
    for p in provenance.values():
        if p.get("run"):
            runs[p["run"]["run_id"]] = p["run"]
    rows = [
        [
            "Collected",
            _utc(r.get("run_started_at")),
            f"{r.get('actor_id') or 'collector'} ({r.get('tier')})",
            f"Run {r['run_id'][:8]} · query: {r.get('query') or '—'} · "
            f"status {r.get('run_status')}",
        ]
        for r in sorted(runs.values(), key=lambda r: r.get("run_started_at") or generated)
    ]
    models = sorted(
        {
            f"{i['classification']['model']} · prompt {i['classification']['prompt_version']}"
            for i in items
        }
    )
    rows.append(["Classified", "See items", "Traced content analytics", "; ".join(models)])
    rows.append(
        [
            "Exported",
            _utc(generated),
            "Traced alert centre",
            f"{len(items)} evidence items; saved raw records hashed at export and attached",
        ]
    )
    pdf.grid(["Event", "Time", "Tool", "Detail"], rows, (22, 38, 44, 72))


def _evidence_item(pdf: EvidencePDF, n: int, item: dict):
    pdf.add_page()
    co, at, pr, cl, cx, re_, pa = (
        item["content"],
        item["attribution"],
        item["preservation"],
        item["classification"],
        item["context"],
        item["reach"],
        item["pattern"],
    )
    pdf.start_section(f"Evidence item E-{n}", level=1)
    pdf.font(18, "B")
    pdf.set_text_color(*INK)
    title = f"Evidence item E-{n}"
    top = pdf.get_y()
    pdf.cell(pdf.get_string_width(title) + 4, 9, title)
    pdf.set_xy(pdf.get_x() + 2, top + 1.8)
    pdf.pill("Candidate for review", YELLOW, INK)
    pdf.set_xy(pdf.get_x() + 2, top + 1.8)
    pdf.pill(cl["category"], YELLOW, INK)
    pdf.set_xy(pdf.l_margin, top + 12)

    pdf.heading("Content evidence", 2)
    pdf.kv(
        [
            ("Post URL", co["url"]),
            ("Post ID", co["post_id"]),
            ("Posted", _utc(co["posted_at"])),
        ],
        links={0: co["url"]},
    )
    pdf.box(co["text"] or "", fill=SOFT, bar=INK, size=10.5)

    pdf.heading("Attribution evidence", 2)
    name = f"@{at['handle']}" + (f" ({at['display_name']})" if at["display_name"] else "")
    followers = _number(at["followers"]) + (
        "  ·  platform verification flag present" if at["verified"] else ""
    )
    pdf.kv([("Account", name), ("Platform user ID", at["user_id"]), ("Followers", followers)])

    pdf.heading("Classification evidence", 2)
    rows = [("Possible X rule", cl["platform_rule"]), ("Category", ", ".join(cl["all_categories"]))]
    if cl["claim"]:
        rows.append(("Model-extracted claim", cl["claim"]))
    if cl["protected_group"]:
        rows.append(("Model-identified target", cl["protected_group"]))
    rows += [
        ("Reason", cl["explanation"]),
        (
            "Assessment",
            f"score {cl['score']:.2f} (candidate threshold met) · {cl['model']} · "
            f"prompt {cl['prompt_version']}",
        ),
    ]
    pdf.kv(rows)

    if cx["subject_posts"]:
        pdf.heading("Linked context (source and relevance unverified)", 2)
        for r in cx["subject_posts"]:
            pdf.box(
                f"{r['text']}\n{_utc(r['posted_at'])} · {r['url']}",
                fill=PRIMARY_LIGHT,
                bar=PRIMARY,
                size=9.5,
            )

    pdf.heading("Recorded engagement (not audience reach)", 2)
    pdf.kv(
        [
            (
                "Engagement at collection",
                f"{_number(re_['views'])} views · {_number(re_['likes'])} likes · "
                f"{_number(re_['reposts'])} reposts · {_number(re_['replies'])} replies",
            )
        ]
    )
    if pa["target"]:
        pdf.heading("Pattern evidence", 2)
        pdf.kv(
            [
                ("Person targeted", pa["target"]),
                (
                    "Model-identified pattern",
                    "Unknown"
                    if pa["repeated_targeting"] is None
                    else "Present"
                    if pa["repeated_targeting"]
                    else "Not marked",
                ),
            ]
        )

    pdf.heading("Preservation evidence", 2)
    pdf.kv(
        [
            ("Collected", _utc(pr["collected_at"])),
            ("Saved raw record", pr["capture_file"] or "Not attached"),
            ("SHA-256", pr["capture_sha256"]),
            ("Archive copy", pr["archive_url"] or "None (recommended)"),
        ]
    )
    pdf.heading("Evidence checklist", 2)
    for c in item["checks"]:
        mark, color = ("✓", OK) if c["met"] else (("✗", RED) if c["required"] else ("–", MUTED))
        pdf.font(10, "B")
        pdf.set_text_color(*color)
        pdf.cell(6, 5.6, mark if pdf.unicode else ("OK" if c["met"] else "-"))
        pdf.font(10)
        pdf.set_text_color(*INK)
        label = c["element"] + ("" if c["required"] else " (optional)")
        pdf.multi_cell(
            0,
            5.6,
            pdf.clean(f"{label} — {c['detail']}"),
            align="L",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )


def _accounts(pdf: EvidencePDF, accounts):
    pdf.add_page()
    pdf.heading("5. Accounts with grouped model candidates")
    if not accounts:
        pdf.para(
            "No grouped accounts are available in this draft.",
            color=MUTED,
        )
        return
    pdf.grid(
        ["Account", "Platform user ID", "Candidates", "Labels", "Categories"],
        [
            [
                f"@{a['handle']}",
                a["platform_user_id"],
                a["ready"],
                a["violations"],
                ", ".join(CATEGORY_LABELS[c] for c in a["categories"]),
            ]
            for a in accounts
        ],
        (36, 44, 18, 22, 56),
    )


def _integrity(pdf: EvidencePDF, items, manifest_hash):
    pdf.heading("6. Integrity and verification")
    pdf.para(
        "Each selected saved raw record is attached to this PDF (open the attachments panel of a "
        "PDF reader). To verify, save the attachment and compute its SHA-256, for example with  "
        "shasum -a 256 <file>  (macOS/Linux) or  certutil -hashfile <file> SHA256  (Windows). "
        "The result must equal the value below."
    )
    pdf.ln(2)
    rows = [
        [f"E-{n}", i["preservation"]["capture_file"], i["preservation"]["capture_sha256"]]
        for n, i in enumerate(items, 1)
    ]
    rows.append(["Manifest", "evidence.json", manifest_hash])
    pdf.grid(["Item", "Attachment", "SHA-256"], rows, (18, 64, 94))


def _preparation(pdf: EvidencePDF, settings):
    pdf.add_page()
    pdf.heading("7. Preparation and review")
    blank = "______________________________"
    pdf.kv(
        [
            ("Prepared by", settings.notifier_name or blank),
            ("Email", settings.notifier_email or blank),
            ("Organisation", settings.notifier_organisation or blank),
        ]
    )
    pdf.heading("Preparation statement", 2)
    pdf.box(PREPARER_STATEMENT)
    pdf.heading("Review", 2)
    pdf.box(REVIEW_NOTE, fill=YELLOW_LIGHT, bar=YELLOW)
    pdf.ln(12)
    y = pdf.get_y()
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.3)
    for x, label in ((20, "Prepared by (name, signature)"), (112, "Reviewed by (name, signature)")):
        pdf.line(x, y, x + 84, y)
        pdf.set_xy(x, y + 1.5)
        pdf.font(8.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(84, 4, label)
    pdf.set_y(y + 16)
    y = pdf.get_y()
    for x, label in ((20, "Date"), (112, "Date")):
        pdf.line(x, y, x + 84, y)
        pdf.set_xy(x, y + 1.5)
        pdf.cell(84, 4, label)
    pdf.set_y(y + 8)


# --------------------------------------------------------------------------- entry point


def build_pdf(
    case: dict,
    assessments: list[Assessment],
    references: dict,
    provenance: dict[str, dict],
    accounts: list[dict],
    settings: AlertSettings,
    *,
    generated: datetime | None = None,
) -> tuple[bytes, str, str]:
    """Return (pdf bytes, filename, sha256 of the pdf)."""
    if not assessments or len({a.id for a in assessments}) != len(assessments):
        raise ValueError("Choose distinct saved records for the draft.")
    fresh = [assess(a.row, settings, references) for a in assessments]
    if any(a is None or a.status != READY or not is_supported_platform(a.row) for a in fresh):
        raise ValueError(
            "Each selection must be an available candidate for the supported X platform."
        )
    assessments = fresh
    if not case.get("id") or any(a.row.get("case_id") != case["id"] for a in assessments):
        raise ValueError("Every selected record must belong to the selected case.")
    if set(provenance) != {a.id for a in assessments}:
        raise ValueError("Saved captures do not match the selected records.")
    generated = generated or datetime.now(UTC)
    captures = {k: v["raw"] for k, v in provenance.items()}
    items = [evidence_item(a, references, captures.get(a.id)) for a in assessments]
    authors = {a.row.get("author_id") for a in assessments}
    accounts = [x for x in accounts if x["author_id"] in authors and x["status"] == "ready"]
    manifest = {
        "kind": "traced-digital-evidence-report/v2",
        "generated_at": generated.isoformat(),
        "case": {k: case.get(k) for k in ("id", "slug", "subject_name", "subject_role")},
        "prepared_by": {
            "name": settings.notifier_name,
            "email": settings.notifier_email,
            "organisation": settings.notifier_organisation,
        },
        "screening_settings": settings.model_dump(mode="json"),
        "evidence_types": [{"type": t, "description": d} for t, d in EVIDENCE_TYPES],
        "collection_runs": [p["run"] for p in provenance.values() if p.get("run")],
        "accounts": accounts,
        "items": items,
    }
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False, default=str).encode()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    report_id = f"ER-{generated:%Y%m%d}-{manifest_hash[:8].upper()}"

    pdf = EvidencePDF(report_id, case.get("subject_name") or "", manifest_hash)
    pdf.set_title(f"Digital evidence report {report_id}")
    pdf.set_subject(f"Draft evidence candidates about {case.get('subject_name')}")
    pdf.set_author(settings.notifier_name or "Traced")
    pdf.set_creator("Traced alert centre")
    pdf.set_keywords("draft evidence, X, model proposals, SHA-256")
    pdf.set_creation_date(generated)
    pdf.set_lang("en-US")

    _cover(pdf, case, items, accounts, settings, generated, manifest_hash)
    _summary(pdf, items, accounts)
    _evidence_types(pdf)
    _method(pdf, items, provenance, generated)
    pdf.add_page()
    pdf.heading("4. Evidence items")
    pdf.para(f"{len(items)} evidence items follow, one per page.", color=MUTED)
    for n, item in enumerate(items, 1):
        _evidence_item(pdf, n, item)
    _accounts(pdf, accounts)
    _integrity(pdf, items, manifest_hash)
    _preparation(pdf, settings)
    if pdf.replaced_glyphs:
        pdf.para(
            "Note: characters the report fonts cannot print (such as emoji) are shown as □ "
            "or ?. Verify the displayed extracted text against the attached saved record.",
            size=8.5,
            color=MUTED,
        )

    for item in items:
        raw = captures.get(item["id"])
        if raw is not None:
            pdf.embed_file(
                bytes=raw.encode("utf-8"),
                basename=item["preservation"]["capture_file"].split("/")[-1],
                desc=f"Saved raw record, post {item['content']['post_id']}",
                modification_date=generated,
            )
    # Include every referenced raw record so each exported reference resolves to saved bytes.
    context_ids = sorted({r["id"] for item in items for r in item["context"]["subject_posts"]})
    for ref_id in context_ids:
        pdf.embed_file(
            bytes=references[ref_id]["raw"].encode("utf-8"),
            basename=f"context-{ref_id}.json",
            desc=f"Saved context record {ref_id}",
            modification_date=generated,
        )
    pdf.embed_file(
        bytes=manifest_bytes,
        basename="evidence.json",
        desc="Evidence manifest",
        modification_date=generated,
    )
    data = bytes(pdf.output())
    slug = case.get("slug") or "case"
    return data, f"evidence-report-{slug}-{report_id}.pdf", hashlib.sha256(data).hexdigest()
