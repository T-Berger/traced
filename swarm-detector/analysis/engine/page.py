"""Tiny HTML page builder: sections, KaTeX maths, Plotly figures with a hover 'what to look for'."""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

PLOTLY = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"
KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist"

NAV = [("01_preprocessing.html", "1 · Preprocessing"), ("02_models.html", "2 · Models & theory"),
       ("03_metrics.html", "3 · Metric evaluation"), ("04_webs.html", "4 · Webs")]

COLORS = ["#2a6fdb", "#e8833a", "#2e9e6b", "#c94f7c", "#7b61c9", "#8a8f98", "#c9a227", "#3aa6b9",
          "#a0522d", "#5b8c2a", "#d64545", "#4b5563"]

CSS = """
:root{--ink:#1d2330;--mute:#5d6676;--line:#e3e6ec;--bg:#fafbfc;--card:#fff;--accent:#2a6fdb;--warn:#b7791f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,
"Segoe UI",Inter,sans-serif}main{max-width:1180px;margin:0 auto;padding:24px 20px 80px}
nav{position:sticky;top:0;z-index:5;background:#fffffff2;border-bottom:1px solid var(--line);backdrop-filter:blur(6px)}
nav div{max-width:1180px;margin:0 auto;padding:10px 20px;display:flex;gap:18px;flex-wrap:wrap}
nav a{color:var(--mute);text-decoration:none;font-weight:500}nav a.on{color:var(--accent)}
h1{font-size:28px;margin:18px 0 4px}h2{font-size:21px;margin:44px 0 8px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:16px;margin:26px 0 6px}.sub{color:var(--mute);margin:0 0 18px}
.toc{columns:2;font-size:14px}.toc a{color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}
.fighead{display:flex;justify-content:space-between;align-items:center;gap:10px}.fighead h4{margin:0;font-size:15px}
.cap{color:var(--mute);font-size:13px;margin:4px 0 0}
.hint{position:relative;flex:none;font-size:12px;color:var(--accent);border:1px solid #c9d8f5;background:#f2f6fd;
border-radius:999px;padding:2px 10px;cursor:help}
.hint .tip{display:none;position:absolute;right:0;top:130%;width:min(420px,80vw);background:#1d2330;color:#fff;
padding:10px 12px;border-radius:8px;font-size:13px;line-height:1.5;z-index:10;box-shadow:0 6px 24px #0003}
.hint:hover .tip,.hint:focus .tip{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpi b{display:block;font-size:22px}.kpi span{color:var(--mute);font-size:13px}
.math{background:#f6f7f9;border-left:3px solid var(--accent);padding:8px 14px;margin:10px 0;overflow-x:auto}
.note{background:#fff8e6;border-left:3px solid var(--warn);padding:8px 14px;margin:10px 0}
.plain{background:#eef7f1;border-left:3px solid #2e9e6b;padding:8px 14px;margin:10px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:14px}
table{border-collapse:collapse;font-size:13px;width:100%;margin:8px 0}th,td{border-bottom:1px solid var(--line);
padding:5px 8px;text-align:left;vertical-align:top}th{background:#f3f5f8;position:sticky;top:0}
.num{text-align:right;white-space:nowrap}.tbl{overflow-x:auto;max-height:560px}
code{background:#f0f2f5;padding:1px 5px;border-radius:4px;font-size:13px}
@media(max-width:600px){.grid2{grid-template-columns:1fr}.toc{columns:1}}
"""


def esc(s) -> str:
    return html.escape(str(s))


class Page:
    def __init__(self, filename: str, title: str, subtitle: str):
        self.filename, self.title, self.subtitle = filename, title, subtitle
        self.parts: list[str] = []
        self.toc: list[tuple[str, str]] = []

    def h2(self, title: str) -> None:
        anchor = f"s{len(self.toc) + 1}"
        self.toc.append((anchor, title))
        self.parts.append(f'<h2 id="{anchor}">{esc(title)}</h2>')

    def h3(self, title: str) -> None:
        self.parts.append(f"<h3>{esc(title)}</h3>")

    def html(self, raw: str) -> None:
        self.parts.append(raw)

    def p(self, raw: str) -> None:
        self.parts.append(f"<p>{raw}</p>")

    def math(self, latex: str) -> None:
        # escape so "<" in e.g. \prod_{l<k} is not parsed as a tag; KaTeX reads the decoded text
        self.parts.append(f'<div class="math">$${html.escape(latex, quote=False)}$$</div>')

    def note(self, raw: str) -> None:
        self.parts.append(f'<div class="note">{raw}</div>')

    def plain(self, raw: str) -> None:
        self.parts.append(f'<div class="plain"><b>In plain words.</b> {raw}</div>')

    def kpis(self, items: list[tuple[str, str]]) -> None:
        self.parts.append('<div class="kpis">' + "".join(
            f'<div class="kpi"><b>{v}</b><span>{esc(k)}</span></div>' for k, v in items) + "</div>")

    def fig(self, fig: go.Figure, title: str, look_for: str, caption: str = "", height: int = 380) -> str:
        fig.update_layout(height=height, margin=dict(l=50, r=20, t=20, b=45), template="plotly_white",
                          font=dict(size=12), legend=dict(orientation="h", y=-0.18))
        body = fig.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False})
        cap = f'<p class="cap">{caption}</p>' if caption else ""
        block = (f'<div class="card"><div class="fighead"><h4>{esc(title)}</h4>'
                 f'<span class="hint" tabindex="0">what to look for ⓘ<span class="tip">{look_for}</span></span>'
                 f"</div>{body}{cap}</div>")
        self.parts.append(block)
        return block

    def grid(self, blocks: list[str]) -> None:
        for b in blocks:
            self.parts.remove(b)
        self.parts.append('<div class="grid2">' + "".join(blocks) + "</div>")

    def table(self, df: pd.DataFrame, fmt: dict | None = None, index: bool = True) -> None:
        if index:
            df = df.reset_index()
        num = {c for c in df if pd.api.types.is_numeric_dtype(df[c])}
        fmt = fmt or {}

        def cell(col, v):
            text = "" if pd.isna(v) else fmt[col].format(v) if col in fmt else str(v)
            return f'<td class="{"num" if col in num else ""}">{esc(text)}</td>'

        head = "".join(f'<th class="{"num" if c in num else ""}">{esc(c)}</th>' for c in df)
        body = "".join("<tr>" + "".join(cell(c, v) for c, v in zip(df.columns, row, strict=True)) + "</tr>"
                       for row in df.itertuples(index=False))
        self.parts.append(f'<div class="tbl"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')

    def save(self, folder: Path) -> Path:
        nav = "".join(f'<a href="{f}" class="{"on" if f == self.filename else ""}">{esc(t)}</a>' for f, t in NAV)
        toc = "".join(f'<li><a href="#{a}">{esc(t)}</a></li>' for a, t in self.toc)
        doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(self.title)}</title>
<link rel="stylesheet" href="{KATEX}/katex.min.css"><script src="{PLOTLY}"></script>
<script defer src="{KATEX}/katex.min.js"></script>
<script defer src="{KATEX}/contrib/auto-render.min.js" onload="renderMathInElement(document.body,
{{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'\\\\(',right:'\\\\)',display:false}}]}})"></script>
<style>{CSS}</style></head><body><nav><div>{nav}</div></nav><main>
<h1>{esc(self.title)}</h1><p class="sub">{self.subtitle}</p><ul class="toc">{toc}</ul>
{"".join(self.parts)}</main></body></html>"""
        path = folder / self.filename
        path.write_text(doc, encoding="utf-8")
        return path
