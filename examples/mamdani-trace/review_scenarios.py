"""Render evidence review prompts from the saved Mamdani dashboard observations."""

from __future__ import annotations

from html import escape

from investigation_icons import signal_chip
from source_spans import SourceSpan


def render_scenarios(payload, excerpt, trace_file=None):
    """Link each prompt to exact fragments checked by the dashboard renderer."""
    observations = {item["id"]: item for item in payload.get("exact_source_observations", ())}
    ballot = observations.get("source-observation-1")
    conditional = observations.get("source-observation-3")
    rhetoric = observations.get("source-observation-5")
    rhetoric_refs = {ref["native_id"]: ref for ref in rhetoric["source_refs"]} if rhetoric else {}
    october_post = rhetoric_refs.get("1982948828480180593")
    cards = []

    if ballot:
        ballot_quotes = "".join(excerpt(ref) for ref in ballot["source_refs"])
        ballot_trace = (
            f'<p><a href="{escape(trace_file)}#focus-ballot">Trace the saved ballot post</a></p>'
            if trace_file is not None
            else ""
        )
        allegation = ""
        if conditional and october_post:
            asserted = {
                "record_id": october_post["record_id"],
                "span": SourceSpan(
                    193, 224, "4fe31337c69559685b7c09ff9a7696554ff33754b8388751410ab36ca784a5a6"
                ),
            }
            conditional_quotes = "".join(excerpt(ref) for ref in conditional["source_refs"])
            allegation = (
                '<details class="db-trace-item"><summary>Compare an assertion with a conditional</summary>'
                "<p>Compare the October 27 post with the conditional wording in the saved press release. "
                "What evidence would establish whether information was withheld?</p>"
                f"{excerpt(asserted)}{conditional_quotes}"
                "<p>The records preserve the attributed statements. The underlying filing is unavailable in this case.</p>"
                "</details>"
            )
        cards.append(
            '<article class="card db-lane db-review-scenario" id="misinformation-scenario" '
            'aria-labelledby="misinformation-scenario-title">'
            f"{signal_chip('Claim check · draft', 'document', 'review')}"
            '<h3 id="misinformation-scenario-title">Which part of the ballot claim holds up?</h3>'
            "<p>The certified recap lists Mamdani on two party lines in a vote-for-one contest. "
            "Compare the repeated listing, the allegation of fraud, and New York’s counting rule.</p>"
            '<details class="db-trace-item"><summary>Read the claim and election records</summary>'
            f"{ballot_quotes}{ballot_trace}"
            "<p>The excerpts establish the listing and the counting rule. An independent tally audit would address how votes were counted.</p>"
            "</details>"
            f"{allegation}</article>"
        )

    dated_posts = (
        (
            "1982948828480180593",
            "October 27 · Before Election Day",
            "Religion and an allegation about Mamdani",
        ),
        (
            "2000057460623630848",
            "December 13 · After Election Day",
            "Political label applied to a supporter",
        ),
        (
            "2005787444743119321",
            "December 29 · After Election Day",
            "Political labels and a religious reference",
        ),
    )
    if all(native_id in rhetoric_refs for native_id, _, _ in dated_posts):
        examples = "".join(
            f'<div class="db-trace-item"><strong>{date_label}</strong>'
            f"<p>{frame}</p>{excerpt(rhetoric_refs[native_id])}</div>"
            for native_id, date_label, frame in dated_posts
        )
        cards.append(
            '<article class="card db-lane db-review-scenario" id="hateful-rhetoric-scenario" '
            'aria-labelledby="hateful-rhetoric-scenario-title">'
            f"{signal_chip('Rhetoric review · pending', 'target', 'review')}"
            '<h3 id="hateful-rhetoric-scenario-title">Who or what does the rhetoric target?</h3>'
            "<p>Compare three posts attributed to @RepOgles across Election Day. "
            "Identify who is being described, what is said about religion, and where political labels are used.</p>"
            '<details class="db-trace-item"><summary>Read the three dated excerpts</summary>'
            f"{examples}"
            "<p>Read each saved post and check any attached context before assessing protected-trait targeting. "
            "These three selected posts provide examples for review.</p>"
            "</details></article>"
        )

    return "".join(cards)
