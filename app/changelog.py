"""The update log shown when you click the version number: CHANGELOG.md, turned into simple HTML.

Only the small part of Markdown the changelog uses is understood: "## version" headings, paragraphs,
bullet lists one level deep (continuation lines indented), **bold**, `code` and [links](url)."""
import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
REPO = "https://github.com/EvilEd3/Payday/blob/main/"  # where relative links in the changelog point


def _inline(text: str) -> str:
    out = str(escape(text))
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if not re.match(r"https?://", url):
            if "://" in url or url.startswith(("/", "#")) or ":" in url.split("/")[0]:
                return label  # nothing but plain web links and files in the repository
            url = REPO + url
        return f'<a href="{url}" rel="noopener" target="_blank">{label}</a>'
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, out)


def _render(lines: list[str]) -> str:
    """One section's lines as HTML: paragraphs and (nested) bullet lists."""
    blocks: list[tuple[str, list]] = []  # ("p", [text]) or ("ul", [[text, [subitems]]])
    for line in lines:
        if not line.strip():
            if blocks and blocks[-1][0] == "p":
                blocks.append(("gap", []))
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if body.startswith("- "):
            if indent >= 2 and blocks and blocks[-1][0] == "ul" and blocks[-1][1]:
                blocks[-1][1][-1][1].append(body[2:])
            else:
                if not blocks or blocks[-1][0] != "ul":
                    blocks.append(("ul", []))
                blocks[-1][1].append([body[2:], []])
        elif indent and blocks and blocks[-1][0] == "ul" and blocks[-1][1]:
            item = blocks[-1][1][-1]
            if item[1]:
                item[1][-1] += " " + body
            else:
                item[0] += " " + body
        elif blocks and blocks[-1][0] == "p":
            blocks[-1][1].append(body)
        else:
            blocks.append(("p", [body]))
    html = []
    for kind, content in blocks:
        if kind == "p":
            html.append(f"<p>{_inline(' '.join(content))}</p>")
        elif kind == "ul":
            items = []
            for text, subs in content:
                sub = "".join(f"<li>{_inline(s)}</li>" for s in subs)
                items.append(f"<li>{_inline(text)}{f'<ul>{sub}</ul>' if sub else ''}</li>")
            html.append(f"<ul>{''.join(items)}</ul>")
    return "".join(html)


def parse(text: str) -> list[dict]:
    """[{"version": "1.1.0", "html": Markup}, ...], newest first, as the file lists them.
    An empty "Unreleased" section is left out."""
    releases, current = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            current = {"version": line[3:].strip(), "lines": []}
            releases.append(current)
        elif current is not None:
            current["lines"].append(line)
    out = []
    for r in releases:
        html = _render(r["lines"])
        if html:
            out.append({"version": r["version"], "html": Markup(html),
                        "unreleased": r["version"].lower() == "unreleased"})
    return out


@lru_cache(maxsize=1)
def releases() -> list[dict]:
    try:
        return parse(CHANGELOG.read_text(encoding="utf-8"))
    except OSError:
        return []
