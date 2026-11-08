"""Alert centre: threshold alerts and takedown evidence from labeled Postgres analytics.

The labels are produced outside Traced (``analytics_*`` tables written by the
content-analytics classifier). Traced reads them read-only, applies the reviewer's
saved thresholds, and prepares evidence packages. Labels are model-assisted
assessments that a reviewer confirms before filing a report.
"""
