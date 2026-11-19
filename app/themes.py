"""Prebuilt colour themes. Each has a light and a dark set; people pick Auto, Light or Dark separately.

The token names are what the stylesheet uses:
  bg / surface / sunk   page, cards, wells          ink / muted / line   text, quiet text, borders
  bush (+soft)          money you have, the accent  flax (+soft)         bills coming, pending
  rata (+soft)          over budget, missed          on-bush              text on an accent-coloured tile
  trend                 the day-rate line            c1..c8               chart colours (c1 follows the accent)
"""
import hashlib

SHARED_LIGHT = {"c2": "#C8892A", "c3": "#4F7CAC", "c4": "#A8402F", "c5": "#7A6AA8", "c6": "#6E9C3E", "c7": "#B5679A", "c8": "#8A9396"}
SHARED_DARK = {"c2": "#E3B25E", "c3": "#82A9D6", "c4": "#E8836F", "c5": "#A99BD6", "c6": "#A2CB72", "c7": "#DB97C2", "c8": "#A9B3B5"}

THEMES = {
    "bush": {
        "label": "Bush", "about": "Greens and mist. The original.",
        "light": {"bg": "#F1F4F2", "surface": "#FFFFFF", "sunk": "#E8EDEA", "ink": "#14211D", "muted": "#586964", "line": "#DCE3DF",
                  "bush": "#21624C", "bush-soft": "#D7E8E0", "flax": "#8A5A0B", "flax-soft": "#F4E7CB",
                  "rata": "#A53C2B", "rata-soft": "#F6DFD9", "on-bush": "#F2FAF6", "trend": "#4F7CAC", "c1": "#2E6B57"},
        "dark": {"bg": "#0A1110", "surface": "#121C1A", "sunk": "#0D1614", "ink": "#E4ECE9", "muted": "#8FA29D", "line": "#22302D",
                 "bush": "#74C3A3", "bush-soft": "#183129", "flax": "#E6B866", "flax-soft": "#352B18",
                 "rata": "#EC8B78", "rata-soft": "#3B221D", "on-bush": "#0A1110", "trend": "#82A9D6", "c1": "#6FBE9E"},
    },
    "harbour": {
        "label": "Harbour", "about": "Cool blues and grey.",
        "light": {"bg": "#E8EDF2", "surface": "#F7F9FC", "sunk": "#DCE4EC", "ink": "#121F2D", "muted": "#536376", "line": "#CBD6E1",
                  "bush": "#1D5C88", "bush-soft": "#D3E3F0", "flax": "#8A5A0B", "flax-soft": "#F2E4C6",
                  "rata": "#A53C2B", "rata-soft": "#F5DCD6", "on-bush": "#F5F9FD", "trend": "#B5679A", "c1": "#1D5C88"},
        "dark": {"bg": "#0C1520", "surface": "#132131", "sunk": "#08101A", "ink": "#E1E9F2", "muted": "#91A2B5", "line": "#23364A",
                 "bush": "#7DB5E2", "bush-soft": "#1A3450", "flax": "#E6B866", "flax-soft": "#3A2F1A",
                 "rata": "#EC8B78", "rata-soft": "#40241F", "on-bush": "#0C1520", "trend": "#DB97C2", "c1": "#7DB5E2"},
    },
    "tussock": {
        "label": "Tussock", "about": "Warm sand and olive.",
        "light": {"bg": "#F1ECE2", "surface": "#FBF8F2", "sunk": "#E7E0D2", "ink": "#2A241A", "muted": "#6C6252", "line": "#DCD3C2",
                  "bush": "#56631A", "bush-soft": "#E3E6CB", "flax": "#9A4E12", "flax-soft": "#F5DFC9",
                  "rata": "#A3303B", "rata-soft": "#F5D9DB", "on-bush": "#FBF8F2", "trend": "#4F7CAC", "c1": "#56631A"},
        "dark": {"bg": "#16130E", "surface": "#201C15", "sunk": "#100D09", "ink": "#EFE8DA", "muted": "#AA9F8C", "line": "#3A3326",
                 "bush": "#BCC77A", "bush-soft": "#2F3416", "flax": "#EBA86A", "flax-soft": "#3D2A17",
                 "rata": "#EE8B92", "rata-soft": "#442126", "on-bush": "#16130E", "trend": "#82A9D6", "c1": "#BCC77A"},
    },
    "ironsand": {
        "label": "Ironsand", "about": "High contrast black and white, teal accent.",
        "light": {"bg": "#F4F5F5", "surface": "#FFFFFF", "sunk": "#E8EAEA", "ink": "#050707", "muted": "#3F4646", "line": "#BFC5C5",
                  "bush": "#0A6158", "bush-soft": "#D2EAE6", "flax": "#7A4A00", "flax-soft": "#F4E2C2",
                  "rata": "#9E1F12", "rata-soft": "#F7D8D3", "on-bush": "#FFFFFF", "trend": "#5B3FB0", "c1": "#0A6158"},
        "dark": {"bg": "#000000", "surface": "#111313", "sunk": "#000000", "ink": "#F7F9F9", "muted": "#B7BFBF", "line": "#3A4141",
                 "bush": "#5FDBCB", "bush-soft": "#0F332E", "flax": "#FFC867", "flax-soft": "#3A2A0C",
                 "rata": "#FF8E80", "rata-soft": "#43150F", "on-bush": "#000000", "trend": "#B9A6FF", "c1": "#5FDBCB"},
    },
    "twilight": {
        "label": "Twilight", "about": "Soft violet and slate.",
        "light": {"bg": "#ECEAF2", "surface": "#FAF9FD", "sunk": "#E1DEEA", "ink": "#1D1A2B", "muted": "#5E5A72", "line": "#D4D0E1",
                  "bush": "#54479C", "bush-soft": "#E0DCF3", "flax": "#8A5A0B", "flax-soft": "#F2E4C6",
                  "rata": "#A53C2B", "rata-soft": "#F5DCD6", "on-bush": "#FAF9FD", "trend": "#C8892A", "c1": "#54479C"},
        "dark": {"bg": "#12101B", "surface": "#1B1828", "sunk": "#0C0A13", "ink": "#ECE9F6", "muted": "#A29DBA", "line": "#2F2B42",
                 "bush": "#B6AAF3", "bush-soft": "#2C2550", "flax": "#E6B866", "flax-soft": "#3A2F1A",
                 "rata": "#EC8B78", "rata-soft": "#40241F", "on-bush": "#12101B", "trend": "#E3B25E", "c1": "#B6AAF3"},
    },
}
DEFAULT = "bush"
MODES = {"auto": "Match this device", "light": "Light", "dark": "Dark"}


def _mix(a: str, b: str, t: float) -> str:
    """a blended into b by t (0..1), as hex."""
    pa, pb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * t + y * (1 - t)):02X}" for x, y in zip(pa, pb))


def _block(tokens: dict, shared: dict, dark: bool) -> str:
    shadow = ("0 1px 2px rgb(0 0 0 / .4), 0 12px 32px rgb(0 0 0 / .45)" if dark
              else "0 1px 2px rgb(20 33 29 / .05), 0 10px 30px rgb(20 33 29 / .09)")
    props = {**shared, **tokens}
    # The Overview's pay card: the accent itself in light mode, a deep tint of it in dark mode
    props["hero"] = _mix(tokens["bush"], tokens["surface"], .20) if dark else tokens["bush"]
    props["on-hero"] = tokens["ink"] if dark else tokens["on-bush"]
    body = " ".join(f"--{k}: {v};" for k, v in props.items())
    return f"{body} --shadow: {shadow}; color-scheme: {'dark' if dark else 'light'};"


def stylesheet() -> str:
    """Light rules, then dark rules: dark wins in Dark mode, or in Auto mode when the device is dark."""
    out = ["/* Generated from app/themes.py. */"]
    for key, t in THEMES.items():
        light = f":root[data-theme={key}]" + (", :root:not([data-theme])" if key == DEFAULT else "")
        out.append(f"{light} {{ {_block(t['light'], SHARED_LIGHT, False)} }}")
    for key, t in THEMES.items():
        dark = _block(t["dark"], SHARED_DARK, True)
        auto = f":root[data-theme={key}]:not([data-mode=light]):not([data-mode=dark])"
        if key == DEFAULT:
            auto += ", :root:not([data-theme])"
        out.append(f"@media (prefers-color-scheme: dark) {{ {auto} {{ {dark} }} }}")
        out.append(f":root[data-theme={key}][data-mode=dark] {{ {dark} }}")
    return "\n".join(out) + "\n"


CSS = stylesheet()
VERSION = hashlib.sha256(CSS.encode()).hexdigest()[:10]


def browser_colour(theme: str, mode: str) -> dict:
    """Background colours for the phone's address bar."""
    t = THEMES.get(theme, THEMES[DEFAULT])
    return {"light": t["light"]["bg"], "dark": t["dark"]["bg"], "mode": mode}
