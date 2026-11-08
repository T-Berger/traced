"""Editorial topic labels and exact cues from the saved Mamdani case."""

from source_spans import SourceSpan

TOPICS = (
    (
        "ballot-design",
        "Ballot design and voting",
        "document",
        "Compare a fraud allegation with a post describing one voting experience.",
        (
            (
                "1985692613010502114",
                "Makes a fraud allegation",
                SourceSpan(
                    64, 101, "6abcc94949aa317d6421e484512aa428973287c8881ad6af9a4d1a96ba586e37"
                ),
            ),
            (
                "1985336258193662449",
                "Describes a voting experience",
                SourceSpan(
                    52, 93, "886be246ec9148da617e6d00bfece755af5611caf15b7e6f7ead349194c333eb"
                ),
            ),
        ),
    ),
    (
        "religion-and-politics",
        "Religion and political identity",
        "target",
        "Compare claims and labels aimed at a candidate, a supporter, and political groups.",
        (
            (
                "1982948828480180593",
                "Alleges motive",
                SourceSpan(
                    44, 82, "213b4bc18c251665fdae6ae8d95d83671e23c13944d7151ec2f19d74862d1b76"
                ),
            ),
            (
                "2000057460623630848",
                "Alleges a disruption",
                SourceSpan(
                    10, 53, "4a4c2e9330307cb6f7c3018b625dce51e0021936c769fa21f38ec72f64c3fd93"
                ),
            ),
            (
                "2005787444743119321",
                "Comments on an invitation",
                SourceSpan(
                    52, 90, "cde8463f779bdfb8984b1bfbab51019a71d87e27dcf7a45a9efdd4ee1cbf686b"
                ),
            ),
            (
                "1981457004355838183",
                "Reports opposition in a letter",
                SourceSpan(
                    19, 57, "076ec641cca1b5376ae972d2162375a45fbbffbf7fb5480e513d0b0ac701c45c"
                ),
            ),
        ),
    ),
    (
        "small-business",
        "Small business policy",
        "flag",
        "Follow a policy announcement, the posts quoting it, and other saved posts on the topic.",
        (
            (
                "1994890085477601610",
                "Previews policy changes",
                SourceSpan(
                    105, 147, "5ecc9d1c45defc496e92a99168bcdb91285087e02ab0ac4d23195429b3f8c901"
                ),
            ),
            (
                "1994906376922173580",
                "Offers conditional approval",
                SourceSpan(
                    192, 240, "1446c06ffe8e8e464027d8534fca1ea863d31b37f925b018e01f7fa7b7ca7e60"
                ),
            ),
            (
                "1995082155417092188",
                "Links policy to a Trump meeting",
                SourceSpan(
                    0, 50, "725abe9e038158645d7377f6953a7562cb3d64f50f73750b27033b5e0ad913c2"
                ),
            ),
            (
                "1994890243095314530",
                "Promotes local spots",
                SourceSpan(
                    45, 92, "651622ce362d91952540b6c196053e631c443aefd7d07463f3c61943ad54e7c7"
                ),
            ),
            (
                "1991161598216434089",
                "Promises support for businesses",
                SourceSpan(
                    34, 95, "0b7f982453444c9512a296db6060e5eb614d343f8595ee1fb4131acaa81c8dc6"
                ),
            ),
        ),
    ),
    (
        "political-support",
        "Political support in the news",
        "quote",
        "Read the news headline and the post that quotes it to criticize the timing of reported support.",
        (
            (
                "1984725922528866654",
                "Reports an offer of political support",
                SourceSpan(
                    36, 84, "0d8d534e974c730b1e9a9a97830a5fafc3cbb84a1b7430f3062c450f892f3a5a"
                ),
            ),
            (
                "1985642138177044502",
                "Criticizes the timing of support",
                SourceSpan(
                    26, 71, "d8eb1daadc0213161e13df0103a08f51653f2659c9b60e9b7b09e395fb9a7084"
                ),
            ),
        ),
    ),
    (
        "community-events",
        "Community events",
        "link",
        "Follow a Diwali recap and the separate post that quotes it.",
        (
            (
                "1980606111838355647",
                "Recaps Diwali visits",
                SourceSpan(
                    0, 47, "4b3b30d729f46426fcf8d6ec4fb8a3d6f12a0bc02ee9c846a4923c2bbf24cc55"
                ),
            ),
            (
                "1980606660528804219",
                "Shares a link",
                SourceSpan(
                    0, 23, "e8cb47e5503803c06a2a2389fbb0d2457025a13025107b500a2f26043f1c2e0f"
                ),
            ),
        ),
    ),
    (
        "class-and-spending",
        "Class and spending",
        "stack",
        "Compare two posts that question the candidate’s working-class image.",
        (
            (
                "1980772447696605449",
                "Challenges the candidate’s working-class image",
                SourceSpan(
                    8, 51, "a11d090bf0c49ba421481d949bdfe29c0c0e5cc058ed44fe60c1e69a277d1f22"
                ),
            ),
            (
                "1980780743757377818",
                "Criticizes the candidate’s spending",
                SourceSpan(
                    27, 61, "b42ff9af97b3d9b76dc77175eb60e31a06c78321446c6a6b2c8460f1a7160d7f"
                ),
            ),
        ),
    ),
)
