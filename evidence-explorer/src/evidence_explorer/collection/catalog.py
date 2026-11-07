"""Editable source presets; runtime success, rather than a preset, establishes connectivity."""

PRESETS = (
    ("telegram-official", "telegram", "Telegram announcements", "telegram"),
    ("telegram-tips", "telegram", "Telegram Tips", "TelegramTips"),
    ("telegram-durov", "telegram", "Pavel Durov", "durov"),
    ("youtube-nasa", "youtube", "NASA videos", "UCLA_DiR1FfKNvjuUpBHmylQ"),
    ("youtube-bbc", "youtube", "BBC News videos", "UC16niRr50-MSBwiO3YDb3RA"),
    ("youtube-dw", "youtube", "DW News videos", "UCknLrEdhRCp1aegoMqRaCZg"),
    ("news-bbc-world", "news", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("news-dw", "news", "DW English", "https://rss.dw.com/rdf/rss-en-all"),
    ("news-tagesschau", "news", "Tagesschau", "https://www.tagesschau.de/xml/rss2/"),
    ("news-guardian", "news", "The Guardian · World", "https://www.theguardian.com/world/rss"),
    ("news-nasa", "news", "NASA news", "https://www.nasa.gov/feed/"),
    ("news-un", "news", "UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml"),
    ("news-dlf", "news", "Deutschlandfunk", "https://www.deutschlandfunk.de/nachrichten-100.rss"),
    ("other-mastodon", "other", "Mastodon announcements", "https://mastodon.social/@Mastodon.rss"),
    ("other-hn", "other", "Hacker News", "https://news.ycombinator.com/rss"),
    ("reddit-worldnews", "reddit", "Reddit · worldnews", "worldnews"),
    ("reddit-science", "reddit", "Reddit · science", "science"),
    ("reddit-technology", "reddit", "Reddit · technology", "technology"),
    ("x-bbcworld", "x", "BBC World on X", "BBCWorld"),
    ("x-nasa", "x", "NASA on X", "NASA"),
    ("x-reuters", "x", "Reuters on X", "Reuters"),
)


def catalog() -> list[dict]:
    from .apis import configuration_status

    return [
        {
            "id": key,
            "platform": platform,
            "label": label,
            "locator": locator,
            "content_mode": "article_links" if key == "other-hn" else "feed_content",
            **(
                {"ready": True, "status": "Public source · not checked yet"}
                if platform not in {"x", "reddit"}
                else {
                    "ready": configuration_status(platform)["ready"],
                    "status": configuration_status(platform)["message"],
                }
            ),
        }
        for key, platform, label, locator in PRESETS
    ]
