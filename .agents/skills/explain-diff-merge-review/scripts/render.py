#!/usr/bin/env python3
"""
render.py — render a structured explanation spec into the self-contained HTML
page format used by the `explain-diff-merge-review` skill (canonically at
.agents/skills/explain-diff-merge-review/SKILL.md). A separate copy of the
renderer originally built for the `explain-diff` skill
(.agents/skills/explain-diff/scripts/render.py) — kept independent per this
repo's skill self-containment convention rather than shared, so the two may
drift over time. That renderer in turn adapted a gist by ankitg12
(https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405), whose
approach originates from geoffreylitt
(https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524).

Why this exists: the CSS and page scaffolding are identical across every
invocation of the explain-diff-merge-review skill — only the content (prose,
diagrams, quiz questions) actually changes per PR. Regenerating that
boilerplate by hand every time wastes tokens. This script takes a small JSON
spec with just the content and renders the final page.

Usage:
    uv run python scripts/render.py spec.json [-o output.html]

If -o is omitted, writes to /tmp/YYYY-MM-DD-explanation-<slug>.html, where
<slug> comes from the spec's "slug" field (falling back to a slugified "title").

Spec format (JSON):
{
  "title": "Adding per-tenant rate limits before merge",
  "subtitle": "Prepared 2026-08-11 · PR #58",
  "slug": "tenant-rate-limits",
  "sections": [
    {"id": "ask", "heading": "What you asked for", "html": "<p>...</p>"},
    {"id": "changed", "heading": "What changed and why", "html": "<p>...</p>"},
    {"id": "judgment-calls", "heading": "Where Claude made a call",
     "html": "<div class=\"callout flag\"><strong>Scope added:</strong> rate limiting was not requested, but ...</div>"},
    {"id": "code", "heading": "Code walkthrough", "html": "<pre><code>...</code></pre>"},
    {"id": "double-check", "heading": "Pre-merge double-check",
     "html": "<div class=\"callout flag\">flagged: stacks/react/bun.lock (dependency lockfiles)</div>"}
  ],
  "quiz": [
    {
      "question": "Why was the added rate limiter built on a token bucket rather than a fixed window?",
      "options": [
        {"text": "A fixed window cannot express a per-tenant limit at all.", "correct": false},
        {"text": "A token bucket absorbs short bursts instead of rejecting them at a window boundary.", "correct": true}
      ]
    }
  ]
}

Option order within each quiz question is randomized by the renderer at render time —
list them in whatever order reads naturally when writing the spec; don't try to
manually vary position to "seem random", the script already guarantees it.

The "html" fields are raw HTML — write real markup (headings, <pre> blocks,
tables, and the styled divs listed in the Format section of the
explain-diff-merge-review skill's SKILL.md), not markdown. This keeps the
script a pure template renderer; all the writing judgment (what to explain,
which judgment calls and flagged paths matter) still belongs to the LLM
following the explain-diff-merge-review skill, same as before — this just
removes the repetitive part.
"""
import argparse
import datetime
import html
import json
import random
import re
import sys
from pathlib import Path

CSS = """
  :root {
    --bg: #fafaf8; --fg: #1a1a1a; --accent: #b5541f; --muted: #6b6b6b;
    --code-bg: #282c34; --code-fg: #e6e6e6; --callout-bg: #fff4e8; --border: #e0ddd6;
  }
  body { font-family: Georgia, 'Times New Roman', serif; background: var(--bg); color: var(--fg);
    max-width: 820px; margin: 0 auto; padding: 2rem 1.5rem 6rem; line-height: 1.65; }
  h1 { font-size: 1.9rem; border-bottom: 3px solid var(--accent); padding-bottom: .5rem; }
  h2 { font-size: 1.4rem; margin-top: 3rem; color: var(--accent); }
  h3 { font-size: 1.1rem; margin-top: 1.8rem; }
  code { font-family: 'SF Mono', Consolas, monospace; background: #eee; padding: .1rem .3rem; border-radius: 3px; font-size: .92em; }
  pre { background: var(--code-bg); color: var(--code-fg); padding: 1rem 1.2rem; border-radius: 8px;
    overflow-x: auto; white-space: pre-wrap; font-family: 'SF Mono', Consolas, monospace; font-size: .88rem; line-height: 1.5; }
  pre code { background: none; padding: 0; color: inherit; }
  .callout { background: var(--callout-bg); border-left: 4px solid var(--accent); padding: .9rem 1.2rem;
    border-radius: 0 6px 6px 0; margin: 1.2rem 0; }
  .callout.flag { background: #fef2f2; border-left-color: #dc2626; }
  .toc { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.5rem; margin: 1.5rem 0; }
  .toc a { color: var(--accent); text-decoration: none; }
  .toc ul { margin: .3rem 0; }
  .diagram { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem;
    margin: 1.2rem 0; font-family: 'SF Mono', Consolas, monospace; font-size: .85rem; }
  .flow { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; justify-content: center; padding: .5rem 0; }
  .box { border: 2px solid var(--accent); border-radius: 8px; padding: .6rem 1rem; background: #fdf6ee; text-align: center; min-width: 120px; }
  .box.fail { border-color: #b91c1c; background: #fef2f2; }
  .arrow { font-size: 1.4rem; color: var(--muted); }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .92rem; }
  th, td { border: 1px solid var(--border); padding: .5rem .7rem; text-align: left; }
  th { background: #f0ede6; }
  .quiz-q { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem 1.5rem; margin: 1.2rem 0; }
  .quiz-radio { position: absolute; opacity: 0; width: 0; height: 0; }
  .quiz-opt { display: block; width: 100%; text-align: left; padding: .6rem 1rem; margin: .4rem 0;
    border: 1px solid var(--border); border-radius: 6px; background: #fff; cursor: pointer; font-family: inherit; font-size: .95rem; }
  .quiz-opt:hover { background: #f5f2ec; }
  .quiz-radio:checked + .quiz-opt { border-color: var(--accent); border-width: 2px; }
  .feedback { display: none; margin-top: .6rem; padding: .6rem 1rem; border-radius: 6px; font-size: .9rem; }
  .feedback.correct { background: #ecfdf3; color: #166534; border-left: 3px solid #16a34a; }
  .feedback.incorrect { background: #fef2f2; color: #991b1b; border-left: 3px solid #dc2626; }
  .quiz-radio:checked + .quiz-opt + .feedback { display: block; }
  .badge { display: inline-block; font-size: .75rem; padding: .15rem .5rem; border-radius: 10px; font-family: sans-serif; }
  .badge.new { background: #dcfce7; color: #166534; }
  @media (max-width: 600px) { body { padding: 1rem; } .flow { flex-direction: column; } }
"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(spec: dict) -> str:
    title = spec["title"]
    subtitle = spec.get("subtitle", "")
    sections = spec.get("sections", [])
    quiz = spec.get("quiz", [])

    toc_items = "\n".join(
        f'  <li><a href="#{html.escape(s["id"])}">{html.escape(s["heading"])}</a></li>' for s in sections
    )
    if quiz:
        toc_items += '\n  <li><a href="#quiz">Quiz</a></li>'

    body_sections = "\n\n".join(
        f'<h2 id="{html.escape(s["id"])}">{html.escape(s["heading"])}</h2>\n{s["html"]}' for s in sections
    )

    quiz_html = ""
    if quiz:
        blocks = []
        for qi, q in enumerate(quiz):
            options = list(q["options"])
            random.shuffle(options)
            opts = []
            for oi, o in enumerate(options):
                opt_id = f"quiz-q{qi}-opt{oi}"
                is_correct = o["correct"] is True
                feedback_cls = "correct" if is_correct else "incorrect"
                feedback_text = "✅ Correct." if is_correct else "❌ Not quite — reread the section above."
                opts.append(
                    f'<input type="radio" name="quiz-q{qi}" id="{opt_id}" class="quiz-radio">\n'
                    f'<label for="{opt_id}" class="quiz-opt">{html.escape(o["text"])}</label>\n'
                    f'<div class="feedback {feedback_cls}">{feedback_text}</div>'
                )
            blocks.append(f'<div class="quiz-q">\n<p><strong>{html.escape(q["question"])}</strong></p>\n' + "\n".join(opts) + "\n</div>")
        quiz_html = '<h2 id="quiz">Quiz</h2>\n\n' + "\n\n".join(blocks)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>

<h1>{html.escape(title)}</h1>
{f'<p style="color:var(--muted); margin-top:-.5rem;">{html.escape(subtitle)}</p>' if subtitle else ''}

<div class="toc">
<strong>Contents</strong>
<ul>
{toc_items}
</ul>
</div>

{body_sections}

{quiz_html}

</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path, help="path to the JSON content spec")
    ap.add_argument("-o", "--output", type=Path, default=None, help="output HTML path")
    args = ap.parse_args()

    try:
        raw = args.spec.read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"render.py: cannot read spec {args.spec}: {exc}")

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.exit(f"render.py: spec {args.spec} is not valid JSON: {exc}")

    if not isinstance(spec, dict) or "title" not in spec:
        sys.exit(f"render.py: spec {args.spec} must be a JSON object with a \"title\" key")

    try:
        out_html = render(spec)
    except (KeyError, TypeError) as exc:
        sys.exit(f"render.py: malformed spec {args.spec}: missing or invalid field {exc}")

    if args.output:
        out_path = args.output
    else:
        date_prefix = datetime.date.today().strftime("%Y-%m-%d")
        slug = slugify(spec.get("slug") or spec["title"])
        out_path = Path(f"/tmp/{date_prefix}-explanation-{slug}.html")

    out_path.write_text(out_html, encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
