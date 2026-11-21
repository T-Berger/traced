"""Executive Compliance Audit Report (PDF) filled from the analytics database.

Follows the audit template's format: title, the four filing fields, then sections 1–4 with
the template's headings, labels and applied definitions. Every figure and every account
named is computed from the saved collection. Where the template asserts something the
data does not record (advertising placement), the report states what was recorded instead.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fpdf import XPos, YPos

from ..alerts.pdf_report import INK, LINE, MUTED, PRIMARY, YELLOW, EvidencePDF
from ..alerts.repository import CaseData
from ..alerts.settings import CATEGORY_LABELS, AlertSettings
from . import metrics

TITLE = "EXECUTIVE COMPLIANCE AUDIT REPORT: LEGAL & REGULATORY COMPLIANCE EVIDENCE"
TARGET_ENTITY = "X Corp."
RECIPIENTS = (
    "New York State Office of the Attorney General (NYS OAG) & Federal Trade Commission (FTC)"
)
AUDIT_WINDOW = "October–December 2025 & January–March 2026"
AUTHORITY = (
    "New York Stop Hiding Hate Act (NYS GBS § 1102) | "
    "FTC Act Deceptive Trade Practices (15 U.S.C. § 45)"
)
COVERAGE_ROWS = 10

# Applied definitions, verbatim from the audit template (NYS GBS § 1102(1)(c) categories).
DEFINITIONS = {
    "disinformation": (
        "A derivative of misinformation; the deliberate creation and/or spreading of false or "
        "engineered information with the purpose of harm, evaluated by contrasting factual "
        "information against false/disinformation from non-credible accounts versus credible "
        "sources."
    ),
    "racism": (
        "Negative, hostile, or derogatory rhetoric that targets individuals or groups based on "
        "their race, ethnicity, nationality, or religious identity to demean, exclude, or incite "
        "hatred against them."
    ),
    "harassment": (
        "The malicious, unreciprocated targeting of individuals through high quantities of "
        "content (bursts of posts or continuous replies), designed to humiliate, degrade, or "
        "incite abuse against them, as supported by X's Abusive Behavior guidelines."
    ),
    "hate_speech": (
        "The dissemination of socioeconomic prejudice designed to create negative sentiment, "
        "hostility, or systemic violence on the basis of race, ethnicity, national origin, caste, "
        "sexual orientation, gender, gender identity, religious affiliation, age, disability, or "
        "serious disease."
    ),
}
SECTION_ORDER = [
    ("A", "disinformation"),
    ("B", "racism"),
    ("C", "harassment"),
    ("D", "hate_speech"),
]


# --------------------------------------------------------------------------- formatting


def _n(v) -> str:
    return "n/a" if v is None else f"{int(v):,}"


def _pct(part, whole) -> str:
    return "n/a" if not whole else f"{100 * part / whole:.1f}%"


def _x(v) -> str:
    return "n/a" if v is None else f"{v:.2f}×"


def _d(ts) -> str:
    return ts.strftime("%d %b %Y") if ts else "not recorded"


def _days(v) -> str:
    return "n/a" if v is None else f"{v:.0f} days"


class AuditPDF(EvidencePDF):
    """The evidence PDF's fonts and tables, with plain running headers and page numbers."""

    def header(self):
        if self.page_no() == 1:
            return
        self.set_fill_color(*PRIMARY)
        self.rect(0, 0, self.w, 3, style="F")
        self.set_y(9)
        self.font(8)
        self.set_text_color(*MUTED)
        self.cell(0, 4, self.clean(f"Executive Compliance Audit Report · {TARGET_ENTITY}"),
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)  # fmt: skip
        self.set_y(self.t_margin)

    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)
        self.font(7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")

    def labeled(self, label: str, text: str, size: float = 10):
        """'Label: text' paragraph, the template's field style."""
        self.font(size)
        self.set_text_color(*INK)
        self.multi_cell(0, 5.2, f"**{self.clean(label)}:** {self.clean(text)}", markdown=True,
                        align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)  # fmt: skip
        self.ln(1.5)


# --------------------------------------------------------------------------- facts


def build_facts(data: CaseData, settings: AlertSettings) -> dict[str, Any]:
    """Every number the report prints, computed once so the PDF and its data file agree."""
    audit = metrics.build_audit(data.labels, settings, data.bursts)
    weeks = metrics.weekly_series(audit)
    return {
        "audit": audit,
        "summary": metrics.summary(audit),
        "matrix": {r["category"]: r for r in metrics.category_matrix(audit)},
        "weeks": weeks,
        "spikes": [w["week"] for w in weeks if w["spike"]],
        "coverage": metrics.account_coverage(audit),
        "high": metrics.high_velocity(audit, settings.high_reach_views),
        "review": metrics.at_review_score(audit, settings),
        "examples": {c: metrics.top_posts(audit.in_category(c)) for c in CATEGORY_LABELS},
        "bursts": data.bursts,
    }


def _post_record(p: metrics.Post) -> dict[str, Any]:
    c = metrics.best_category(p)
    return {
        "id": p.get("id"),
        "platform_item_id": p.get("platform_item_id"),
        "handle": p.get("handle"),
        "category": c,
        "categories": p.categories,
        "score": p.get(c),
        "views": p.get("view_count"),
        "reposts": p.get("repost_count"),
        "posted_at": p.get("posted_at"),
        "collected_at": p.get("collected_at"),
        "days_live": metrics.days_live(p),
        "url": p.get("url"),
        "content_sha256": p.get("content_sha256"),
    }


# --------------------------------------------------------------------------- sections


def _title_block(pdf: AuditPDF, f):
    s = f["summary"]
    pdf.add_page()
    pdf.set_fill_color(*PRIMARY)
    pdf.rect(0, 0, pdf.w, 44, style="F")
    pdf.set_fill_color(*YELLOW)
    pdf.rect(0, 44, pdf.w, 1.8, style="F")
    pdf.set_xy(20, 14)
    pdf.font(16, "B")
    pdf.set_text_color(255, 255, 255)
    pdf.multi_cell(176, 7.5, pdf.clean(TITLE), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(54)
    pdf.kv([
        ("Target Entity", TARGET_ENTITY),
        ("Filing Recipients", RECIPIENTS),
        ("Audit Window", f"{AUDIT_WINDOW} (third-party posts tracked "
         f"{_d(s['posted_start'])} – {_d(s['posted_end'])}; captured {_d(s['capture_start'])})"),
        ("Statutory Authority", AUTHORITY),
    ], widths=(42, 134))  # fmt: skip


def _legal_basis(pdf: AuditPDF, f):
    s = f["summary"]
    pdf.heading("1. Legal basis & actionable violation")
    pdf.para(
        "Under NYS GBS § 1102, X Corp is legally mandated to file biannual reports disclosing the "
        "precise volume of content flagged and actioned (removed, demonetized, or deprioritized) "
        "across statutory categories."
    )
    pdf.ln(2)
    pdf.para(
        "X Corp's public terms of service and regulatory disclosures state a strict enforcement "
        'posture of "Freedom of Speech, not Freedom of Reach"—claiming that violating content is '
        "algorithmically restricted, de-amplified, or stripped of monetization."
    )
    pdf.ln(2)
    pdf.para(
        f"This audit provides empirical tracking data on {s['posts']:,} third-party posts. "
        f"{s['flagged']:,} posts ({_pct(s['flagged'], s['posts'])}) met a statutory category "
        f"threshold; every one was still publicly available when captured, a median of "
        f"{_days(s['median_days_live'])} after posting, with a median reach of "
        f"{_x(s['reach_index'])} that of unflagged posts and {_x(s['share_index'])} their share "
        "velocity. Where X Corp's NYS OAG filings describe deprioritization and restriction of "
        "such content, this data bears directly on whether those reports are misleading under "
        "GBS § 1102(1)(b)(iii), which carries civil penalties of up to $15,000 per violation "
        "per day."
    )


def _core_metrics(pdf: AuditPDF, f, settings):
    s, high, review = f["summary"], f["high"], f["review"]
    pdf.heading("2. Core compliance metrics summary")
    pdf.labeled(
        "Verified High-Velocity Violations Tracked",
        f"{len(high):,} flagged posts with {settings.high_reach_views:,} or more recorded views "
        f"({_n(sum(p.get('view_count') or 0 for p in high))} views in total); {len(review):,} "
        f"flagged posts meet the stricter "
        f"{settings.categories['racism'].filing_score:.2f} score threshold.",
    )
    pdf.labeled(
        "Total Documented Platform Actions Executed",
        f"0 of {s['flagged']:,} (0.0%). All {s['flagged']:,} flagged posts were publicly "
        "available at capture, with no removal, visibility-limited label or restriction "
        "recorded.",
    )
    pdf.labeled(
        "Platform Reaction Latency Timeline",
        "Content remained publicly available, with no platform action recorded, for a median of "
        f"{_days(s['median_days_live'])} (minimum {_days(s['min_days_live'])}) between posting "
        f"and capture on {_d(s['capture_start'])}. Flagged posts reached a median of "
        f"{_n(s['median_views_flagged'])} views against {_n(s['median_views_baseline'])} for "
        f"unflagged posts (reach index {_x(s['reach_index'])}).",
    )


def _examples_table(pdf: AuditPDF, posts: list[metrics.Post], category: str):
    rows, links = [], {}
    for i, p in enumerate(posts):
        rows.append([
            f"@{p.get('handle') or 'unknown'}",
            f"{p.get(category) or 0:.2f}",
            _n(p.get("view_count")),
            _d(p.get("posted_at")),
            _days(metrics.days_live(p)),
            "Open post",
        ])  # fmt: skip
        links[(i, 5)] = p.get("url")
    pdf.grid(["Account", "Score", "Views", "Posted", "Live at capture", "Link"], rows,
             (40, 16, 26, 28, 32, 24), links)  # fmt: skip


def _tracking_text(category: str, f) -> str:
    r, s = f["matrix"][category], f["summary"]
    if category == "harassment":
        bursts = f["bursts"]
        lead = (
            f"{r['posts']:,} posts met the harassment threshold. " if r["posts"] else
            "No individual post met the harassment threshold. "
        )  # fmt: skip
        if not bursts:
            return lead + "No burst campaigns were logged."
        return lead + (
            f"Swarm cluster analytics identified and logged {len(bursts)} burst campaigns "
            f"({s['burst_posts']} posts) by "
            f"{len({b.get('author_handle') for b in bursts})} accounts, each aimed at a single "
            f"target within a short window, between {_d(min(b['window_start'] for b in bursts))} "
            f"and {_d(max(b['window_start'] for b in bursts))}. No account lock, safety cooldown "
            "or throttling was recorded in the captured data."
        )
    if not r["posts"]:
        return f"No post met the {CATEGORY_LABELS[category].lower()} threshold."
    top = f["examples"][category][0]
    return (
        f"{r['posts']:,} posts from {r['accounts']:,} accounts met this threshold "
        f"({_n(r['views'])} recorded views; median {_n(r['median_views'])}, peak "
        f"{_n(r['max_views'])}), with {_x(r['reach_index'])} the median reach and "
        f"{_x(r['engagement_index'])} the engagement per view of unflagged posts. The "
        f"highest-reach instance, from @{top.get('handle')} ({_n(top.get('view_count'))} views), "
        "remained "
        f"publicly available {_days(metrics.days_live(top))} after posting. No 'visibility "
        "limited' label or deprioritization restriction was recorded for any of these posts."
    )


def _coverage(pdf: AuditPDF, f):
    accounts = f["coverage"]
    if not accounts:
        return
    tiers = metrics.coverage_tiers(accounts)
    top = max(k for k, n in tiers.items() if n)
    rows = [a for a in accounts if a["coverage"] == top][:COVERAGE_ROWS]
    pdf.heading("Accounts posting across statutory categories", 2)
    pdf.para(
        "Accounts by the number of the four categories their flagged posts span: "
        + ", ".join(f"{k}/4: {n}" for k, n in tiers.items())
        + f". Leading accounts at the highest level reached ({top}/4):",
        size=9.5,
    )
    pdf.grid(
        ["Account", "Coverage", "Flagged posts", "Disinfo.", "Racism", "Hate sp.", "Views"],
        [[f"@{a['handle'] or 'unknown'}", f"{a['coverage']}/4", _n(a["posts"]),
          _n(a["disinformation"]), _n(a["racism"]), _n(a["hate_speech"]), _n(a["views"])]
         for a in rows],
        (40, 20, 24, 20, 20, 20, 32),
    )  # fmt: skip


def _matrix(pdf: AuditPDF, f):
    pdf.add_page()
    pdf.heading("3. Evidence matrix by statutory categories (NYS GBS § 1102(1)(c))")
    pdf.grid(
        ["Category", "Flagged posts", "Accounts", "Views", "Reach index", "Engagement index"],
        [[CATEGORY_LABELS[c], _n(f["matrix"][c]["posts"]), _n(f["matrix"][c]["accounts"]),
          _n(f["matrix"][c]["views"]), _x(f["matrix"][c]["reach_index"]),
          _x(f["matrix"][c]["engagement_index"])] for _, c in SECTION_ORDER],
        (34, 26, 24, 34, 26, 32),
    )  # fmt: skip
    for letter, c in SECTION_ORDER:
        pdf.heading(f"{letter}. {CATEGORY_LABELS[c]}", 2)
        pdf.labeled("Applied Definition", DEFINITIONS[c], size=9.5)
        pdf.labeled("Audit Tracking Data", _tracking_text(c, f))
        if c == "harassment" and f["bursts"]:
            rows = [[f"@{b.get('author_handle') or 'unknown'}", b.get("target") or "unknown",
                     _n(b.get("post_count")), _n(b.get("reply_count")),
                     b["window_start"].strftime("%Y-%m-%d %H:%M"),
                     b["window_end"].strftime("%H:%M") if b.get("window_end") else ""]
                    for b in f["bursts"]]  # fmt: skip
            pdf.grid(["Account", "Target", "Posts", "Replies", "Start (UTC)", "End"], rows,
                     (42, 30, 18, 20, 40, 26))  # fmt: skip
        elif f["examples"][c]:
            _examples_table(pdf, f["examples"][c], c)
    _coverage(pdf, f)


def _data_trace(pdf: AuditPDF, f):
    s, weeks = f["summary"], f["weeks"]
    pdf.add_page()
    pdf.heading("4. Data trace & platform reaction timeline audit")
    both = [w for w in weeks if w["median_flagged_views"] is not None
            and w["median_baseline_views"] is not None]  # fmt: skip
    above = sum(1 for w in both if w["median_flagged_views"] >= w["median_baseline_views"])
    lower = sum(1 for v in metrics.view_samples(f["audit"])["flagged"]
                if v <= (s["median_views_baseline"] or 0))  # fmt: skip
    pdf.labeled(
        "The Reach vs. Restriction Failure",
        f"Tracking impressions and share velocity, flagged posts reached a median of "
        f"{_n(s['median_views_flagged'])} views against {_n(s['median_views_baseline'])} for "
        f"unflagged posts (reach index {_x(s['reach_index'])}), and were reposted "
        f"{_x(s['share_index'])} as often per view. {_pct(lower, s['flagged'])} of flagged posts "
        "sat at or below the unflagged median, against 50% for identical distributions"
        + (": the distribution curves of flagged and un-flagged content closely overlap."
           if s["flagged"] and 0.4 <= lower / s["flagged"] <= 0.65 else
           ": the distribution curves of flagged and un-flagged content differ.")
        + f" Week by week, the flagged median matched or exceeded the unflagged median in "
        f"{above} of {len(both)} weeks.",
    )  # fmt: skip
    active = [w for w in weeks if w["posts"]]
    peak = max(active, key=lambda w: w["flagged"], default=None)
    cv = s["volatility_cv"]
    pdf.labeled(
        "Velocity & Volatility",
        f"Flagged content appeared in {sum(1 for w in active if w['flagged'])} of {len(active)} "
        "posting weeks"
        + (f", peaking in the week of {_d(peak['week'])} with {peak['flagged']} posts "
           f"({_n(peak['flagged_views'])} views)" if peak else "")
        + ". Weekly flagged share coefficient of variation: "
        + ("n/a" if cv is None else f"{cv:.2f}")
        + (f"; spike weeks above the mean + 2σ: {', '.join(_d(w) for w in f['spikes'])}."
           if f["spikes"] else "; no week exceeded the mean + 2σ."),
    )  # fmt: skip
    pdf.grid(
        ["Week of", "Posts", "Flagged", "Share", "Views on flagged", "Spike"],
        [[_d(w["week"]), _n(w["posts"]), _n(w["flagged"]), f"{100 * w['rate']:.1f}%",
          _n(w["flagged_views"]), "Yes" if w["spike"] else ""] for w in active],
        (34, 22, 22, 22, 46, 30),
    )  # fmt: skip
    pdf.labeled(
        "Monetization Verification",
        "Advertising placement alongside these posts was not captured in this dataset; this "
        "audit makes no finding on advertising served next to flagged content.",
    )


# --------------------------------------------------------------------------- build


def build_report(
    data: CaseData, settings: AlertSettings, *, generated: datetime | None = None
) -> tuple[bytes, str, str]:
    """Return (pdf bytes, filename, sha256 of the pdf)."""
    generated = generated or datetime.now(UTC)
    f = build_facts(data, settings)
    if not f["audit"].posts:
        raise ValueError("No classified third-party posts for this subject.")
    data_file = {
        "kind": "traced-compliance-audit/v1",
        "generated_at": generated.isoformat(),
        "case": {k: data.case.get(k) for k in ("id", "slug", "subject_name", "subject_role")},
        "target_entity": TARGET_ENTITY,
        "settings": settings.model_dump(mode="json"),
        "summary": f["summary"],
        "categories": f["matrix"],
        "weeks": f["weeks"],
        "bursts": f["bursts"],
        "account_coverage": f["coverage"],
        "examples": {c: [_post_record(p) for p in ps] for c, ps in f["examples"].items()},
        "high_velocity": [_post_record(p) for p in f["high"]],
    }
    data_bytes = json.dumps(data_file, indent=2, ensure_ascii=False, default=str).encode()

    pdf = AuditPDF("", data.case.get("subject_name") or "", "")
    pdf.set_title("Executive Compliance Audit Report")
    pdf.set_subject(f"Compliance audit of {TARGET_ENTITY} enforcement")
    pdf.set_creator("Traced compliance audit")
    pdf.set_creation_date(generated)
    pdf.set_lang("en-US")

    _title_block(pdf, f)
    _legal_basis(pdf, f)
    _core_metrics(pdf, f, settings)
    _matrix(pdf, f)
    _data_trace(pdf, f)
    pdf.embed_file(bytes=data_bytes, basename="audit.json", desc="Audit data (all figures)",
                   modification_date=generated)  # fmt: skip
    out = bytes(pdf.output())
    slug = data.case.get("slug") or "case"
    return out, f"compliance-audit-{slug}-{generated:%Y%m%d}.pdf", hashlib.sha256(out).hexdigest()
