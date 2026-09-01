"""Renders branded PNG "cards" for new SBCs, Evolutions, and Objectives.

Each card is a single flat image (not a native Discord embed layout) built
from an HTML/CSS template and rendered with Playwright, then attached to the
webhook post (see post_webhook()'s file_bytes/file_name params in bot.py).
The Discord embed itself stays minimal -- title/url/color plus
image: {url: "attachment://<file_name>"} -- all the real information lives
in the rendered card.

Game-version labeling
----------------------
fut.gg encodes the game a player-card asset belongs to as a two-digit
prefix on the asset filename, e.g.:

    .../futgg-player-item-card/26-67304728.<hash>.webp   -> FC 26
    .../futgg-player-item-card/27-xxxxxxxx.<hash>.webp   -> FC 27  (once live)

detect_game_label() reads that prefix off whatever player-card URL(s) an
item has, so a card is labeled "FC 26" or "FC 27" per item, automatically,
with no manual flag to flip. Items whose reward has no player card at all
(a pack-only SBC or Objective reward) have no such signal to read and fall
back to DEFAULT_GAME_LABEL -- bump that env var once fut.gg's live pages
cut over to FC 27 site-wide (historically at/around launch).
"""

import base64
import os
import re
from pathlib import Path

ASSET_DIR = Path(__file__).parent / "assets"
LOGO_PATH = ASSET_DIR / "logo_badge.png"

DEFAULT_GAME_LABEL = os.environ.get("DEFAULT_GAME_LABEL", "FC 26")

_VERSION_RE = re.compile(r"futgg-player-item-card/(\d{2})-\d+\.")


def detect_game_label(*card_urls_or_paths) -> str:
    """Returns "FC {yy}" read from the first card asset URL/path that
    carries a recognizable version prefix; falls back to
    DEFAULT_GAME_LABEL if none of the given values match (or none were
    given -- e.g. a pack-only reward with no player card at all)."""
    for value in card_urls_or_paths:
        if not value:
            continue
        m = _VERSION_RE.search(value)
        if m:
            return f"FC {m.group(1)}"
    return DEFAULT_GAME_LABEL


def _logo_data_uri() -> str:
    data = LOGO_PATH.read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


_LOGO_DATA_URI = None


def logo_data_uri() -> str:
    global _LOGO_DATA_URI
    if _LOGO_DATA_URI is None:
        _LOGO_DATA_URI = _logo_data_uri()
    return _LOGO_DATA_URI


BASE_CSS = """
:root{
  --bg:#0a0605; --panel:#160c0c; --panel2:#1c0f0f; --line:#3a1414;
  --ink:#f6efee; --muted:#c99; --muted2:#a67373;
  --red:#ff2b2b; --red2:#8a0d0d; --red-deep:#2a0606;
}
*{box-sizing:border-box; margin:0; padding:0;}
body{width:1080px; background:var(--bg); font-family:Arial, "Helvetica Neue", sans-serif; color:var(--ink); position:relative;}

.glow{position:absolute; top:-160px; left:50%; transform:translateX(-50%); width:780px; height:520px; border-radius:50%;
  background:radial-gradient(circle, rgba(255,43,43,0.24), rgba(255,43,43,0) 70%); filter:blur(6px); z-index:0;}
.watermark-logo{
  position:absolute; left:50%; bottom:-70px; transform:translateX(-50%); width:380px; height:380px;
  background-image:url('LOGO_URI'); background-size:contain; background-repeat:no-repeat; background-position:center;
  opacity:0.045; z-index:0;
}

.frame{position:relative; z-index:2; padding:36px 40px 0; display:flex; flex-direction:column; align-items:center; text-align:center;}

header{display:flex; flex-direction:column; align-items:center; gap:12px;}
.brand{display:flex; align-items:center; gap:12px;}
.brand .mark{width:36px; height:36px; border-radius:9px; background-image:url('LOGO_URI'); background-size:cover; background-position:center; flex-shrink:0;}
.brand .name{font-size:18px; font-weight:900; letter-spacing:1px;}
.brand .name .sol{color:var(--red);}
.brand .divider{width:1px; height:20px; background:var(--line); margin:0 2px;}
.brand .game{font-size:13px; color:var(--muted); font-weight:700; letter-spacing:1px;}
.tag{background:var(--red); color:#1a0303; font-weight:900; font-size:12.5px; letter-spacing:1.5px; padding:8px 18px; border-radius:8px;}

.eyebrow{margin-top:22px; font-size:13px; letter-spacing:4px; color:var(--muted); font-weight:700; text-transform:uppercase;}
h1{font-size:38px; font-weight:900; margin-top:8px; line-height:1.05; letter-spacing:-0.5px;}
.sub{font-size:16.5px; color:var(--muted); margin-top:9px; max-width:640px; line-height:1.4;}

.body{display:flex; gap:40px; margin-top:26px; justify-content:center;}
.col{width:460px; display:flex; flex-direction:column; text-align:left;}

.panel{
  background:var(--panel); border:1px solid var(--line); border-radius:14px;
  height:520px; display:flex; flex-direction:column; overflow:hidden;
}
.panel-head{
  padding:16px 22px; border-bottom:1px solid var(--line);
  font-size:12.5px; font-weight:800; letter-spacing:2px; color:var(--red); text-transform:uppercase;
  display:flex; justify-content:space-between; align-items:center;
}
.panel-head .count{color:var(--muted2); font-weight:700; letter-spacing:0;}
.panel-body{flex:1; padding:14px 18px; display:flex; flex-direction:column; justify-content:flex-start; gap:8px;}
.panel-body.centered{justify-content:center;}

.row{
  background:var(--panel2); border:1px solid var(--line); border-radius:10px;
  padding:12px 16px; display:flex; gap:12px; align-items:flex-start;
  border-left:3px solid var(--line);
}
.row.accent{border-left-color:var(--red);}
.row .icon{
  width:24px; height:24px; border-radius:6px; background:#241010; border:1px solid var(--line);
  display:flex; align-items:center; justify-content:center; font-size:12px; flex-shrink:0; margin-top:1px;
}
.row .label{font-size:14px; font-weight:800; line-height:1.25;}
.row .cond{font-size:11.5px; color:var(--muted2); line-height:1.3; margin-top:2px;}

.subhead{
  padding:10px 18px 4px; font-size:11px; font-weight:800; letter-spacing:1.5px; color:var(--muted2); text-transform:uppercase;
}

.reward-panel-body{flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px;}

.card-photo{
  width:150px; height:210px; border-radius:14px; position:relative;
  background:var(--panel2) center/cover no-repeat;
  border:1px solid var(--red2); overflow:hidden;
}
.card-photo.mini{width:110px; height:150px; border:1px solid var(--line);}
.card-photo img{width:100%; height:100%; object-fit:cover;}
.card-cap{text-align:center;}
.card-cap .t{font-size:15px; font-weight:800;}
.card-cap .s{font-size:12px; color:var(--muted); margin-top:3px;}

.arrow{font-size:26px; color:var(--red); font-weight:900;}
.evo-pair{display:flex; align-items:center; gap:18px;}

.pack{
  width:150px; height:210px; border-radius:14px; position:relative;
  background:var(--panel2) center/cover no-repeat;
  border:1px solid var(--red2); display:flex; align-items:center; justify-content:center;
}
.pack .glyph{font-size:44px;}

footer{
  position:relative; z-index:2; width:100%;
  margin:28px 0 0; padding:22px 40px; display:flex; flex-direction:column; align-items:center; gap:14px;
  background:var(--panel);
  border-top:1px solid var(--line);
}
.foot-left{display:flex; align-items:center; gap:16px;}
.foot-icon{
  width:50px; height:50px; border-radius:13px; background:var(--red);
  display:flex; align-items:center; justify-content:center; font-size:21px; color:#1a0303; flex-shrink:0;
}
.foot-title{font-size:18px; font-weight:900; text-align:left;}
.foot-desc{font-size:13px; color:var(--muted); margin-top:3px; max-width:480px; text-align:left;}
.foot-right{display:flex; gap:10px; flex-shrink:0;}
.chip{
  background:var(--panel2); border:1px solid var(--line); border-radius:8px;
  padding:10px 18px; font-size:13px; font-weight:800; white-space:nowrap; color:var(--ink);
}
.chip.status{color:#ff8a6e; border-color:#7a2a1c;}
.chip.status.ok{color:#8ef0ac; border-color:#1c6b3a;}

.sitewatermark{position:relative; z-index:2; text-align:center; padding:14px 40px 22px; font-size:12px; color:var(--muted2); letter-spacing:1px;}
"""


def row(icon: str, label: str, cond: str, accent: bool = False) -> str:
    cls = "row accent" if accent else "row"
    return (
        f'<div class="{cls}"><div class="icon">{icon}</div>'
        f'<div><div class="label">{label}</div><div class="cond">{cond}</div></div></div>'
    )


def panel(head_label: str, count_label: str, rows_html: str, centered: bool = False) -> str:
    body_cls = "panel-body centered" if centered else "panel-body"
    return (
        f'<div class="panel"><div class="panel-head"><span>{head_label}</span>'
        f'<span class="count">{count_label}</span></div>'
        f'<div class="{body_cls}">{rows_html}</div></div>'
    )


def frame(
    game_label: str,
    category_label: str,
    title: str,
    sub: str,
    left_html: str,
    right_html: str,
    foot_icon: str,
    foot_title: str,
    foot_desc: str,
    chip1: str,
    chip2_html: str,
) -> str:
    css = BASE_CSS.replace("LOGO_URI", logo_data_uri())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{css}</style></head>
<body>
  <div class="glow"></div>
  <div class="watermark-logo"></div>
  <div class="frame">
    <header>
      <div class="brand">
        <div class="mark"></div>
        <div class="name">FUT <span class="sol">SOLUTIONS</span></div>
        <div class="divider"></div>
        <div class="game">{game_label}</div>
      </div>
      <div class="tag">{category_label}</div>
    </header>

    <div class="eyebrow">{game_label} · ULTIMATE TEAM</div>
    <h1>{title}</h1>
    <p class="sub">{sub}</p>

    <div class="body">
      <div class="col">{left_html}</div>
      <div class="col">{right_html}</div>
    </div>
  </div>

  <footer>
    <div class="foot-left">
      <div class="foot-icon">{foot_icon}</div>
      <div>
        <div class="foot-title">{foot_title}</div>
        <div class="foot-desc">{foot_desc}</div>
      </div>
    </div>
    <div class="foot-right">
      <div class="chip">{chip1}</div>
      {chip2_html}
    </div>
  </footer>
  <div class="sitewatermark">discord.gg/futsolutions · fut-solutions.com</div>
</body></html>"""


def render_card(page, html: str) -> bytes:
    """Renders `html` (a full document, e.g. from frame()) to PNG bytes
    using an already-open Playwright page. Callers are expected to reuse
    one page across a run rather than launching a new browser per card --
    see bot.py's main(), which opens one extra page alongside the one used
    for scraping."""
    page.set_content(html, wait_until="load")
    page.wait_for_timeout(50)
    return page.screenshot(full_page=True)
