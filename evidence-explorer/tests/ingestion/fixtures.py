"""Small realistic pages and feeds; all requests use mocked transports."""

import json

PUBLIC_IP = "93.184.216.34"
QUOTE = "The bridge remains closed to vehicles until engineers finish their inspection."


def article_html(*, published=True, title="Rivergate bridge inspection update"):
    metadata = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": title,
        "author": {"@type": "Person", "name": "Alex Example"},
        "publisher": {"@type": "Organization", "name": "Example Newsroom"},
    }
    published_tag = ""
    if published:
        metadata["datePublished"] = "2026-11-01T09:30:00+01:00"
        published_tag = (
            '<meta property="article:published_time" content="2026-11-01T09:30:00+01:00">'
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><title>{title}</title>
<meta property="og:title" content="{title}">
<meta property="og:site_name" content="Example Newsroom">
<meta name="author" content="Alex Example">
{published_tag}
<script type="application/ld+json">{json.dumps(metadata)}</script>
</head><body>
<nav>Subscribe to our newsletter | Account sign in</nav>
<main><article><h1>{title}</h1>
<p>Rivergate transport officials published an update after engineers inspected the main bridge
following heavy rain. Residents had requested a clear timetable for the next safety assessment.</p>
<p>{QUOTE} The authority said that pedestrian access would be reviewed separately.</p>
<p>Inspectors are checking the bridge foundations and recording changes in river levels.
The council has not announced a reopening date. A further public update is expected after
the inspection team completes its report.</p>
</article></main><footer>Privacy policy | Advertising</footer></body></html>"""


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example Newsroom</title>
<link>https://news.example/</link><description>Public updates</description>
<item><title>Bridge inspection</title><link>/articles/bridge?utm_source=rss</link>
<guid>bridge-1</guid><pubDate>Sun, 01 Nov 2026 08:30:00 GMT</pubDate></item>
<item><title>Duplicate bridge entry</title><link>https://news.example/articles/bridge#update</link>
<guid>bridge-duplicate</guid></item>
<item><title>Harbor tide update</title><link>/articles/tide</link><guid>tide-2</guid></item>
</channel></rss>"""


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://news.example/">
<title>Example Newsroom</title><id>https://news.example/feed</id>
<updated>2026-11-01T10:00:00Z</updated>
<entry><title>Bridge inspection</title><id>bridge-1</id>
<link rel="alternate" href="articles/bridge?utm_source=atom"/>
<published>2026-11-01T08:30:00Z</published></entry>
<entry><title>Harbor tide update</title><id>tide-2</id>
<link rel="alternate" href="/articles/tide"/>
<updated>2026-11-01T10:00:00Z</updated></entry>
</feed>"""
