# Policy guidelines

Reference material for the **Policies** tab in the integrated desk. It explains how to
report disinformation and abuse to each platform, which platform rule applies, and what
evidence to keep. Everything here comes from official sources: platform help centres,
platform filings, and government pages. Each entry records the URL and the date it was
last checked.

```
policy-guidelines/
  common/how-to-document.json   evidence types and capture steps (all platforms)
  regulations/*.json            government rules relevant to reporting (e.g. NY Stop Hiding Hate Act)
  twitter/platform.json         X: rules, enforcement model, reporting steps, official sources
  twitter/documents/            official documents (X's terms-of-service reports filed with the NY AG)
  tiktok/platform.json          TikTok: same structure
  tiktok/documents/
  cases/*.json                  case guides (e.g. Zohran Mamdani): categories, examples, trusted sources
```

## Keeping it current

1. Use official sources only: the platform's own help centre or rules pages, its filings,
   or government sites. Third-party guides can point you to a page, but cite the official
   page itself.
2. When you check an entry, update its `verified` date. Use `method` to say how you
   checked it (for example, "fetched" or "search index" when the site blocks automated
   access).
3. Put new official PDFs in the platform's `documents/` folder and list them under
   `regulatory_reports` with the official URL.
4. To add a platform, copy `twitter/platform.json` into a new folder (for example
   `instagram/`). To add a case, copy `cases/zohran-mamdani.json`. The Policies tab picks
   up new files after a refresh.

The page reads this folder from the project root. To use a different location, set
`TRACED_POLICY_DIR`.
