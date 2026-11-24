"""The update log behind the version number."""
from app.changelog import CHANGELOG, parse
from app.version import __version__

SAMPLE = """# Changelog

Intro that isn't shown.

## Unreleased

## 1.1.0

**Before updating:** set `PUBLIC_URL`
first.

- **Guide** ([docs/truenas.md](docs/truenas.md)): installing
  on TrueNAS.
- **Fixes**:
  - One <script>
  - Two, see [site](https://example.com)

## 1.0.0

First release.
"""


def test_parse_sections_lists_and_inline():
    out = parse(SAMPLE)
    assert [r["version"] for r in out] == ["1.1.0", "1.0.0"]  # empty Unreleased left out
    html = str(out[0]["html"])
    assert "<p><strong>Before updating:</strong> set <code>PUBLIC_URL</code> first.</p>" in html
    assert 'href="https://github.com/EvilEd3/Payday/blob/main/docs/truenas.md"' in html
    assert "installing on TrueNAS." in html  # continuation line joined
    assert "<ul><li>One &lt;script&gt;</li><li>Two, see <a href=\"https://example.com\"" in html  # nested, escaped
    assert str(out[1]["html"]) == "<p>First release.</p>"


def test_unsafe_links_are_plain_text():
    html = str(parse("## 1.0\n\n- [x](javascript:alert(1)) and [y](//evil.example)\n")[0]["html"])
    assert "href" not in html


def test_real_changelog_lists_this_version():
    versions = [r["version"] for r in parse(CHANGELOG.read_text(encoding="utf-8"))]
    assert __version__ in versions


def test_updates_page_open_without_signing_in(client):
    r = client.get("/updates")
    assert r.status_code == 200
    assert "Your version" in r.text and __version__ in r.text
    assert 'href="/updates"' in r.text  # the footer's version number links here
