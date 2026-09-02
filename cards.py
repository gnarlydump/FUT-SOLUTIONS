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
CURRENCY_ICON_DIR = ASSET_DIR / "currency"

# Which bundled icon goes with which currency label. Matched on the label
# the card already prints, so adding a currency means adding a file and a
# line here -- nothing else has to know about it.
_CURRENCY_ICONS = {
    "coins": "coins.png",
    "fc points": "fc_points.png",
    "points": "fc_points.png",
}
_CURRENCY_URI_CACHE: dict[str, str] = {}


def currency_icon_uri(label: str) -> str:
    """The bundled icon for a currency label, inlined, or "" if we have
    none. Never raises: a missing icon file costs the icon, not the card
    (see _logo_data_uri for why that matters)."""
    key = (label or "").strip().lower()
    name = _CURRENCY_ICONS.get(key)
    if not name:
        return ""
    if key not in _CURRENCY_URI_CACHE:
        try:
            data = (CURRENCY_ICON_DIR / name).read_bytes()
            _CURRENCY_URI_CACHE[key] = (
                "data:image/png;base64," + base64.b64encode(data).decode("ascii")
            )
        except OSError:
            _CURRENCY_URI_CACHE[key] = ""
    return _CURRENCY_URI_CACHE[key]

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
    """The FUT Solutions badge, inlined so the render needs no network.

    A missing file must NOT raise. This is called from frame() on every
    card, and process_category() skips any item whose card fails to
    render -- so when logo_badge.png went missing from the deployed repo,
    every SBC, evolution and objective silently stopped posting. The
    watermark and the footer mark are decoration; losing them is worth a
    slightly plainer card, never a card that never posts."""
    try:
        data = LOGO_PATH.read_bytes()
    except OSError as e:
        print(f"  ! logo asset missing ({LOGO_PATH}): {e} -- rendering without it")
        # A 1x1 transparent PNG: every background-image rule still
        # resolves, so nothing else in the layout has to change.
        return (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


_LOGO_DATA_URI = None


def logo_data_uri() -> str:
    global _LOGO_DATA_URI
    if _LOGO_DATA_URI is None:
        _LOGO_DATA_URI = _logo_data_uri()
    return _LOGO_DATA_URI


PLAYSTYLE_ICON_DIR = ASSET_DIR / "playstyles"

_PLAYSTYLE_ICON_CACHE: dict[str, str | None] = {}


def _playstyle_slug(name: str) -> str:
    return name.strip().lower().rstrip("+").strip().replace(" ", "_")


# A PlayStyle name is a couple of short words ("Aerial Fortress"). Anything
# longer than this isn't one, and must never reach a filesystem lookup --
# payloads also carry description text and base64 image data URIs, and
# building a path out of those raises "File name too long".
MAX_PLAYSTYLE_NAME_LEN = 40


def _resolve_icon_slug(slug: str) -> str | None:
    """Maps a PlayStyle name onto one of our bundled icon files, tolerating
    the naming differences between fut.gg surfaces -- a player page may say
    "Long Ball" where the PlayStyles reference calls it "Long Ball Pass".
    Tries an exact match, then a unique prefix match either direction, and
    gives up (returning None, so the caller shows a plain marker) rather
    than guessing between two equally-good candidates.

    Matching is done against an in-memory list rather than by probing the
    filesystem, so an arbitrary string from a payload can't produce an
    OSError however long or strange it is."""
    if not slug or len(slug) > MAX_PLAYSTYLE_NAME_LEN:
        return None
    available = playstyle_vocabulary()
    if slug in available:
        return slug
    matches = [a for a in available if a.startswith(slug) or slug.startswith(a)]
    return matches[0] if len(matches) == 1 else None


def _icon_data_uri(filename: str) -> str | None:
    """Returns a base64 data URI for a bundled, unmodified fut.gg PlayStyle
    icon crop (see assets/playstyles/), or None if we don't have that one
    -- callers fall back to a plain marker in that case rather than fail
    the whole card."""
    if filename in _PLAYSTYLE_ICON_CACHE:
        return _PLAYSTYLE_ICON_CACHE[filename]
    path = PLAYSTYLE_ICON_DIR / filename
    uri = None
    if path.exists():
        uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    _PLAYSTYLE_ICON_CACHE[filename] = uri
    return uri


def playstyle_badge(name: str) -> str:
    """Renders just the raw PlayStyle icon graphic, verbatim -- no card
    box, no label, no recoloring or reshaping. A trailing "+" on the name
    (fut.gg's convention for the upgraded/elite tier, e.g. "Quick Step+")
    uses the real gold-hexagon PlayStyle+ art; a regular PlayStyle uses
    the real white-diamond icon art. Colors are only ever what fut.gg's
    own art already is."""
    is_plus = name.strip().endswith("+")
    slug = _resolve_icon_slug(_playstyle_slug(name))
    if slug is None:
        print(f"  ! no PlayStyle icon bundled for '{name.strip()}'")
        return '<span class="fallback">★</span>' if is_plus else '<span class="fallback">⬥</span>'
    icon_uri = _icon_data_uri(f"{slug}_plus.png" if is_plus else f"{slug}.png")
    return f'<img class="ps-icon" src="{icon_uri}">' if icon_uri else '<span class="fallback">⬥</span>'


def playstyle_chips(names: list[str], base_names: list[str] | None = None) -> str:
    """PlayStyles as chips, the upgraded (+) tier first and outlined.

    Only the + tier is marked. Base-tier PlayStyles are listed plainly
    whether or not the evolution added them -- the + is the thing worth
    picking out of the row. (base_names is accepted so callers don't have
    to care, but it doesn't change what gets outlined.)"""
    if not names:
        return ""
    ordered = sorted(names, key=lambda n: 0 if n.strip().endswith("+") else 1)
    chips = ""
    for n in ordered:
        label = n.rstrip("+").strip()
        is_plus = n.strip().endswith("+")
        cls = "role gained" if is_plus else "role"
        plus = "<b>+</b>" if is_plus else ""
        chips += f'<div class="{cls}">{label}{plus}</div>'
    return chips


def playstyle_badges(names: list[str]) -> str:
    """PlayStyle+ (gold hexagon) badges are shown first, regular PlayStyles
    after -- matching fut.gg's own display order -- with each group
    otherwise kept in its original order."""
    if not names:
        return ""
    ordered = sorted(names, key=lambda n: 0 if n.strip().endswith("+") else 1)
    chips = "".join(playstyle_badge(n) for n in ordered)
    return f'<div class="player-section-label">PlayStyles</div><div class="diamond-grid">{chips}</div>'


def _first_present(d: dict, *keys):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return None


def _playstyle_vocabulary() -> dict[str, str]:
    """Maps a normalized slug -> canonical display name for every PlayStyle
    we have icon art for. Built from the bundled assets, so the vocabulary
    and the icon set can never drift apart."""
    vocab = {}
    for path in PLAYSTYLE_ICON_DIR.glob("*.png"):
        if path.stem.endswith("_plus"):
            continue
        vocab[path.stem] = path.stem.replace("_", " ").title()
    return vocab


_VOCAB = None


def playstyle_vocabulary() -> dict[str, str]:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = _playstyle_vocabulary()
    return _VOCAB


def extract_playstyle_names(player: dict) -> list[str]:
    """Finds a player's PlayStyles anywhere in their payload, without
    depending on fut.gg's field names.

    Rather than guessing at keys like "playStyles" (which is brittle -- the
    scraper has already been broken once by fut.gg renaming things), this
    walks the whole player object and keeps any value that matches a known
    PlayStyle name. The vocabulary is derived from our own bundled icon
    set, so a match is only ever a PlayStyle we can actually draw, and
    unrelated strings in the payload can't be mistaken for one.

    Tier is decided by context, in priority order: an explicit plus flag on
    the entry, a "+" already in the name, or the key path it was found
    under containing "plus" (fut.gg groups the elite tier separately). A
    PlayStyle found in both a plus and a regular context is kept as plus.

    Returns canonical names ("Long Ball Pass", not "long ball"), with a
    trailing "+" on elite-tier entries, in vocabulary-independent order:
    the order they appear in the payload."""
    if not player:
        return []

    vocab = playstyle_vocabulary()
    found: dict[str, bool] = {}          # slug -> is_plus
    order: list[str] = []

    def note(slug: str, is_plus: bool):
        if slug not in found:
            order.append(slug)
            found[slug] = is_plus
        elif is_plus:
            found[slug] = True           # plus context wins over regular

    def match(value) -> str | None:
        # Payloads are full of long free text and base64 image data; a
        # PlayStyle name is short and word-like, so cheap shape checks come
        # before any lookup.
        if not isinstance(value, str) or not value or len(value) > MAX_PLAYSTYLE_NAME_LEN:
            return None
        if any(ch in value for ch in "/:;{}<>\n"):
            return None
        slug = _playstyle_slug(value)
        if slug in vocab:
            return slug
        resolved = _resolve_icon_slug(slug)
        return resolved if resolved in vocab else None

    def walk(node, path_has_plus: bool):
        if isinstance(node, dict):
            explicit_plus = any(
                bool(node.get(k)) for k in ("isPlus", "plus", "isPlayStylePlus", "upgraded")
            )
            name_val = node.get("name") or node.get("label") or node.get("title")
            slug = match(name_val)
            if slug:
                plus = explicit_plus or str(name_val).strip().endswith("+") or path_has_plus
                note(slug, plus)
                return
            for key, value in node.items():
                walk(value, path_has_plus or "plus" in str(key).lower())
        elif isinstance(node, list):
            for entry in node:
                walk(entry, path_has_plus)
        else:
            slug = match(node)
            if slug:
                note(slug, path_has_plus or str(node).strip().endswith("+"))

    walk(player, False)
    return [vocab[s] + ("+" if found[s] else "") for s in order]


def extract_positions(player: dict) -> tuple[str | None, list[str]]:
    """Best-effort (primary_position, alt_positions) extraction -- same
    unverified-field-name caveat as extract_playstyle_names()."""
    if not player:
        return None, []
    primary = _first_present(player, "position", "primaryPosition")
    alts_raw = _first_present(player, "alternativePositions", "altPositions", "positions") or []
    alts = [a for a in alts_raw if isinstance(a, str)] if isinstance(alts_raw, list) else []
    return primary, alts


def extract_stats(player: dict) -> dict[str, int] | None:
    """Best-effort PAC/SHO/PAS/DRI/DEF/PHY extraction -- same
    unverified-field-name caveat as extract_playstyle_names()."""
    if not player:
        return None
    keys = [
        ("PAC", "pace", "pac"), ("SHO", "shooting", "sho"), ("PAS", "passing", "pas"),
        ("DRI", "dribbling", "dri"), ("DEF", "defending", "def"), ("PHY", "physicality", "physical", "phy"),
    ]
    stats = {}
    for label, *variants in keys:
        v = _first_present(player, *variants)
        if v is None:
            return None  # partial stat lines look broken; skip the whole row instead
        stats[label] = v
    return stats


def position_versatility_row(player: dict) -> str:
    primary, alts = extract_positions(player)
    if not primary and not alts:
        return ""
    chips = ""
    if primary:
        chips += f'<div class="pos-chip primary">{primary} (Primary)</div>'
    chips += "".join(f'<div class="pos-chip">{p}</div>' for p in alts)
    return f'<div class="player-section-label">Position Versatility</div><div class="pos-row">{chips}</div>'


def role_familiarity_row(player: dict) -> str:
    """The player's roles for the panel layout (SBC / objective reward),
    highest familiarity first.

    Roles have two levels, + and ++, and never appear bare -- see
    extract_roles(). Returns "" when the payload carries none, so the
    section disappears rather than rendering an empty heading."""
    names = extract_roles(player)
    if not names:
        return ""
    ordered = sorted(names, key=lambda n: -n.count("+"))
    chips = "".join(
        f'<div class="role">{n.rstrip("+")}<b>{"+" * n.count("+")}</b></div>'
        for n in ordered
    )
    return f'<div class="player-section-label">Roles</div><div class="pos-row">{chips}</div>'


def player_stat_row(player: dict) -> str:
    stats = extract_stats(player)
    if not stats:
        return ""
    cells = "".join(f'<div><span class="v">{v}</span>{k}</div>' for k, v in stats.items())
    return f'<div class="stat-row">{cells}</div>'


# Where the rating and the six face stats sit on an FC player card, as
# fractions of the card image. Measured on two different card designs (an
# icon and a special), which agreed to within ~1.5%.
STAT_COL_CENTRES = (0.178, 0.297, 0.419, 0.535, 0.661, 0.779)
STAT_ROW_TOP = 0.708


def evolved_card(img_url: str, base: dict, upgraded: dict,
                 already_painted: bool = False) -> str:
    """The player's card with the evolved rating and face stats painted
    over the printed base ones -- the card reads as the post-evolution
    card, which is what someone deciding whether to start an evo wants to
    see. Stats the evolution doesn't raise are left untouched, so the
    card's own number shows through."""
    if not img_url:
        return '<div class="card-photo"></div>'
    if already_painted:
        # The numbers are composited into the artwork itself; nothing to
        # overlay.
        return f'<div class="evo-card"><img src="{img_url}"></div>'
    patches = ""
    up_ovr, base_ovr = upgraded.get("overall"), base.get("overall")
    if isinstance(up_ovr, int) and up_ovr != base_ovr:
        patches += (
            '<div class="ovr-patch" style="left:8.5%; top:10.5%; width:20%;'
            ' height:9%; font-size:22px;">' + str(up_ovr) + "</div>"
        )
    up_stats, base_stats = extract_stats(upgraded), extract_stats(base) or {}
    if up_stats:
        for i, (key, value) in enumerate(up_stats.items()):
            if i >= len(STAT_COL_CENTRES):
                break
            old = base_stats.get(key)
            if not (isinstance(old, int) and isinstance(value, int) and value > old):
                continue          # untouched: leave the card's own number
            left = STAT_COL_CENTRES[i] - 0.055
            patches += (
                f'<div class="stat-patch" style="left:{left*100:.1f}%;'
                f' top:{STAT_ROW_TOP*100:.1f}%; width:11%; height:4.6%;'
                f' font-size:13px;">{value}</div>'
            )
    return f'<div class="evo-card"><img src="{img_url}">{patches}</div>'


def stat_row_compare(base: dict, upgraded: dict) -> str:
    """The upgraded player's face stats, with any stat the evolution
    actually raises shown in green and annotated with its gain, and
    unchanged stats left plain -- so one card can carry the before/after
    that fut.gg's identical base/upgraded artwork can't.

    Falls back to a plain upgraded stat row when there's nothing to compare
    against, and to nothing at all when the upgraded player carries no
    stats."""
    up = extract_stats(upgraded)
    if not up:
        return ""
    before = extract_stats(base) or {}
    cells = ""
    for key, value in up.items():
        old = before.get(key)
        improved = (
            isinstance(old, int) and isinstance(value, int) and value > old
        )
        cls = ' class="up"' if improved else ""
        delta = f'<span class="d">+{value - old}</span>' if improved else ""
        cells += f'<div{cls}><span class="v">{value}</span>{key}{delta}</div>'
    return f'<div class="stat-row compare">{cells}</div>'


def player_detail_block(player: dict) -> str:
    """Combines the stat row, position row, and PlayStyle badges into one
    block to drop into a reward panel -- each piece independently omits
    itself if that data isn't found, so a partial or empty player dict
    just renders less rather than breaking the card.

    The extract_*() helpers guess at fut.gg's field names (see their
    docstrings). Run once with DEBUG_PLAYER_SCHEMA=true to print the raw
    keys each real reward player actually carries, then correct those
    helpers if the guesses missed."""
    if not player:
        return ""
    if os.environ.get("DEBUG_PLAYER_SCHEMA", "").lower() == "true":
        print(f"  [schema] reward player keys: {sorted(player.keys())}")
        for key in ("playStyles", "playstyles", "traits", "alternativePositions", "positions"):
            if key in player:
                print(f"  [schema]   {key} = {player[key]!r}")
    return (
        player_stat_row(player)
        + position_versatility_row(player)
        + role_familiarity_row(player)
        + playstyle_badges(extract_playstyle_names(player))
    )


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
/* Sits fully inside the card rather than hanging off the bottom edge --
   with the footer bar gone the card is shorter, and a watermark bleeding
   past the edge read as a smudge behind the panels instead of a mark. */
.watermark-logo{
  position:absolute; left:50%; bottom:40px; transform:translateX(-50%); width:300px; height:300px;
  background-image:url('LOGO_URI'); background-size:contain; background-repeat:no-repeat; background-position:center;
  opacity:0.035; z-index:0;
}

.frame{position:relative; z-index:2; padding:36px 40px 0; display:flex; flex-direction:column; align-items:center; text-align:center;}

header{display:flex; flex-direction:column; align-items:center; gap:10px;}
.brand-mini{display:flex; align-items:center; gap:8px; opacity:0.6;}
.brand-mini .mark{width:18px; height:18px; border-radius:5px; background-image:url('LOGO_URI'); background-size:cover; background-position:center; flex-shrink:0;}
.brand-mini .name{font-size:11px; font-weight:800; letter-spacing:1.5px; text-transform:uppercase;}
.brand-mini .name .sol{color:var(--red);}
.tag{
  background:linear-gradient(180deg, #ff5c5c, var(--red) 50%, #c81010);
  color:#1a0303; font-weight:900; font-size:22px; letter-spacing:3.5px;
  padding:14px 38px; border-radius:11px;
  box-shadow:0 0 0 2px rgba(255,255,255,0.16) inset, 0 8px 24px rgba(255,43,43,0.65), 0 0 50px rgba(255,43,43,0.5);
  text-shadow:0 1px 0 rgba(255,255,255,0.3);
}

.eyebrow{margin-top:22px; font-size:13px; letter-spacing:4px; color:var(--muted); font-weight:700; text-transform:uppercase;}
h1{font-size:29px; font-weight:700; margin-top:8px; line-height:1.1; letter-spacing:-0.3px; color:#ede4e3;}
.sub{font-size:16.5px; color:var(--muted); margin-top:9px; max-width:640px; line-height:1.4;}

/* Columns stretch to the taller of the two, so panels stay level without
   either of them being pinned to a fixed height -- a card with little to
   show is now short instead of half empty. */
.body{display:flex; gap:40px; margin-top:26px; justify-content:center; align-items:stretch; width:100%;}
.col{width:460px; display:flex; flex-direction:column; text-align:left;}
/* Used when the reward is just a pack or a generic image: give the detail
   column the room instead of reserving half the card for a placeholder. */
.col.wide{flex:1; width:auto;}
.col.narrow{width:300px; flex:0 0 300px;}

.panel{
  background:var(--panel); border:1px solid var(--line); border-radius:14px;
  display:flex; flex-direction:column; overflow:visible; height:100%;
}
.panel-head{
  padding:16px 22px; border-bottom:1px solid var(--line);
  font-size:12.5px; font-weight:800; letter-spacing:2px; color:var(--red); text-transform:uppercase;
  display:flex; justify-content:space-between; align-items:center;
}
/* min-width:0 and the right alignment matter: a long count (a reward
   named "2x Premium Gold Players Pack") used to run straight over the
   heading instead of wrapping under itself. */
.panel-head .count{color:var(--muted2); font-weight:700; letter-spacing:0; min-width:0; text-align:right; line-height:1.35;}
.panel-head > span:first-child{flex-shrink:0;}
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

/* A denser one-line row for long lists (an evolution's full upgrade list
   can run to a dozen-plus entries) -- label on the left, value on the
   right, so the whole list stays scannable instead of becoming a wall of
   boxes. */
.row.compact{padding:7px 14px; align-items:center; gap:10px;}
.row.compact .icon{width:18px; height:18px; font-size:10px; margin-top:0;}
.row.compact .label{font-size:12.5px; font-weight:700;}
.row.compact .kv{display:flex; align-items:center; justify-content:space-between; flex:1; gap:10px;}
.row.compact .val{font-size:12.5px; font-weight:800; color:#8ef0ac; white-space:nowrap;}
.row.compact .val .cap{color:var(--muted2); font-weight:700; font-size:11px;}

.subhead{
  padding:10px 18px 4px; font-size:11px; font-weight:800; letter-spacing:1.5px; color:var(--muted2); text-transform:uppercase;
}

/* Two columns for a long run of compact rows -- see two_col(). */
.col2{display:grid; grid-template-columns:1fr 1fr; gap:6px;}
.col2 .row.compact{padding:6px 12px;}
.col2 .row.compact .label{font-size:12px;}
.col2 .row.compact .val{font-size:12px;}

/* The evolution's right-hand panel stacks full-width blocks (the overall
   headline, the stat strip) rather than centring a card image, so it must
   stretch instead of inheriting reward-panel-body's centring. */
.panel-stack{flex:1; display:flex; flex-direction:column; align-items:stretch; gap:8px;}
/* Second-level heading inside a section: the upgrade groups (face stats,
   ingame stats, others) sitting under "Upgrades Applied". */
.subhead.sub2{
  padding:8px 18px 2px; font-size:10px; letter-spacing:1.2px; color:var(--red);
  opacity:0.85;
}

/* Top-aligned rather than centred: when the detail column runs long (an
   evolution's full upgrade list, an objective's ten tasks) centring left
   the reward stranded in the middle of a tall empty panel. */
.reward-panel-body{flex:1; display:flex; flex-direction:column; align-items:center; justify-content:flex-start; gap:14px; padding-top:22px;}

.card-photo{
  width:172px; height:210px; border-radius:14px; position:relative;
  background:transparent center/contain no-repeat;
  overflow:hidden;
}
.card-photo.mini{width:123px; height:150px;}
/* The reward on an SBC card, where the art is the panel's whole point and
   nothing is printed under it any more (no caption, no repeated stat row),
   so it gets the space those used to take. */
.card-photo.hero{width:252px; height:308px;}
.card-photo img{width:100%; height:100%; object-fit:contain;}
.card-cap{text-align:center;}
.card-cap .t{font-size:15px; font-weight:800;}
.card-cap .s{font-size:12px; color:var(--muted); margin-top:3px;}

.arrow{font-size:26px; color:var(--red); font-weight:900;}
.evo-pair{display:flex; align-items:flex-start; gap:18px;}

/* fut.gg serves the SAME card image for an evolution's base and upgraded
   player -- only the `overall` differs -- so the artwork alone cannot show
   the upgrade. Each side is labelled and carries its own rating badge, and
   the upgraded side is highlighted, so the before/after is legible even
   though the two pictures are identical. */
.evo-side{display:flex; flex-direction:column; align-items:center; gap:8px;}
.evo-side .side-label{
  font-size:10px; font-weight:800; letter-spacing:1.5px; text-transform:uppercase;
  color:var(--muted2);
}
.evo-side.upgraded .side-label{color:var(--red);}
.evo-ovr{
  display:flex; align-items:baseline; gap:5px;
  font-size:22px; font-weight:900; color:var(--ink); line-height:1;
}
.evo-ovr .unit{font-size:10px; font-weight:800; color:var(--muted2); letter-spacing:1px;}
.evo-side.upgraded .evo-ovr{color:#8ef0ac;}
.evo-delta{font-size:11px; font-weight:800; color:#8ef0ac;}

/* Evolved values painted onto the card itself, where a player expects to
   read them. FC card templates place the rating and the six face stats at
   the same relative positions across designs (measured on an icon card and
   a special card: stat band at y 0.725-0.755 and 0.712-0.747), so these
   percentages hold. Each chip covers the printed base value with the
   evolved one; a stat the evo doesn't raise is left showing the card's own
   number. */
/* The wrapper shrink-wraps the <img>, so percentage positions inside it
   map exactly onto the card artwork. Positioning against a fixed-size box
   instead lets `contain` letterbox the image and throws every patch off. */
.evo-card{position:relative; display:inline-block; line-height:0;}
.evo-card img{height:250px; width:auto; display:block;}
.ovr-patch{
  position:absolute; display:flex; align-items:center; justify-content:center;
  background:rgba(6,3,3,0.92); border-radius:6px;
  font-weight:900; color:#3ddc7f; line-height:1;
}
.stat-patch{
  position:absolute; display:flex; align-items:center; justify-content:center;
  background:rgba(6,3,3,0.92); border-radius:4px;
  font-weight:900; color:#3ddc7f; line-height:1;
}

/* Single-card evolution: the rating carries the before/after that the
   artwork cannot, since fut.gg's base and upgraded images are identical. */
.evo-ovr-line{display:flex; align-items:baseline; gap:7px; margin-top:12px;}
.evo-ovr-line .was{font-size:17px; font-weight:800; color:var(--muted2); text-decoration:line-through;}
.evo-ovr-line .arrow-sm{font-size:14px; color:var(--red); font-weight:900;}
.evo-ovr-line .now{font-size:26px; font-weight:900; color:#8ef0ac; line-height:1;}
.evo-ovr-line .unit{font-size:10px; font-weight:800; color:var(--muted2); letter-spacing:1px;}
.evo-ovr-line .evo-delta{margin-left:4px;}

.pack{
  width:150px; height:210px; border-radius:14px; position:relative;
  background:var(--panel2) center/cover no-repeat;
  border:1px solid var(--red2); display:flex; align-items:center; justify-content:center;
}
.pack .glyph{font-size:44px;}

/* A non-player reward -- a pack, a promo tile, a set graphic. These are
   not card-shaped, so they get their natural aspect ratio and the width
   of the (narrower) reward column, rather than being letterboxed inside
   a portrait card frame and left floating in an empty panel. */
.reward-art{
  width:100%; height:auto; display:block; border-radius:12px;
  border:1px solid var(--line);
}
/* Shown only if the artwork fails to load (see reward_art()) -- a broken
   image icon must never reach Discord. */
.art-wrap{width:100%;}
.art-wrap .pack{display:none; width:100%; height:200px;}

/* Nothing sits under a non-player reward except its name and tradeability,
   so it centres in the panel instead of hugging the top of a column kept
   tall by the challenge list beside it. */
.reward-panel-body.mid{justify-content:center; padding-top:0;}

/* Whether the reward can be sold. EA prints this on the item itself and
   it changes what the reward is worth, so it sits with the reward here
   too -- green for tradeable, muted for untradeable. */
.trade{
  align-self:center; font-size:10.5px; font-weight:900; letter-spacing:1.2px;
  text-transform:uppercase; border-radius:999px; padding:4px 12px;
  border:1px solid var(--line); color:var(--muted2); background:var(--panel2);
}
.trade.yes{color:#8ef0ac; border-color:rgba(61,220,127,0.3); background:rgba(61,220,127,0.07);}

/* Branding sits at the foot of the card, alongside the Discord link,
   so the category tag is the only thing competing for attention up top. */
.sitewatermark{
  position:relative; z-index:2; padding:26px 40px 24px; font-size:12px;
  color:var(--muted2); letter-spacing:1px;
  display:flex; align-items:center; justify-content:center; gap:10px;
}
.sitewatermark .dot{opacity:0.5;}

.stat-row{display:flex; gap:14px; font-size:11px; color:var(--muted); text-align:center; justify-content:center; margin-top:6px;}
.stat-row div{display:flex; flex-direction:column; gap:2px;}
.stat-row .v{color:var(--ink); font-weight:800; font-size:15px;}

/* On an evolution, a stat the evo actually raises is shown in green with
   its gain; untouched stats stay plain, so the upgrade is readable at a
   glance from a single card. */
/* Styled as the card's own stat strip continued underneath it: fut.gg's
   artwork prints the BASE stats, and the numbers sit at different heights
   on different card designs (measured: 70% down an icon card, 92% down a
   special card), so painting over them would misalign. Showing the evolved
   line directly beneath the card is exact on every design. */
.stat-row.compare{
  gap:16px; margin-top:12px; padding:9px 16px;
  background:var(--panel2); border:1px solid var(--line); border-radius:10px;
}
.stat-row.compare .up .v{color:#8ef0ac;}
.stat-row.compare .up{color:#8ef0ac;}
.stat-row.compare .d{font-size:9.5px; font-weight:800; color:#8ef0ac; opacity:0.85; margin-top:1px;}

.player-section-label{font-size:10.5px; font-weight:800; letter-spacing:1.5px; color:var(--muted2); text-transform:uppercase; margin:10px 0 2px; align-self:flex-start;}
.pos-row{display:flex; flex-wrap:wrap; gap:6px; align-self:flex-start;}
.pos-chip{background:var(--panel2); border:1px solid var(--line); border-radius:6px; padding:4px 10px; font-size:11px; font-weight:700;}
.pos-chip.primary{border-color:var(--red2); color:var(--ink);}

/* Role chips in the panel layout. Same shape as a position chip -- they
   sit directly under one -- with the familiarity suffix picked out, since
   Holding+ and Holding++ are different things and the + is the whole
   point of the chip. (The evolution card has its own copy of this rule in
   EVO_CSS, where the chip can also be marked as gained.) */
.roles{display:flex; flex-wrap:wrap; gap:6px; align-self:flex-start;}
.role{background:var(--panel2); border:1px solid var(--line); border-radius:6px;
  padding:4px 10px; font-size:11px; font-weight:700;}
.role b{color:#8ef0ac; font-weight:900;}
/* Marked when the evolution adds this role/PlayStyle or raises its tier. */
.role.gained{border-color:#3ddc7f; color:var(--ink);}

/* The evolved face stats, as a strip: the new value large, the old one
   underneath in small green wherever the evolution actually raised it, so
   what changed is readable without comparing two cards. */
.stat-strip{
  display:flex; gap:10px; width:100%; margin-top:2px; padding:10px 6px;
  background:var(--panel2); border:1px solid var(--line); border-radius:10px;
}
.stat-strip .s{flex:1; display:flex; flex-direction:column; align-items:center; gap:1px;}
.stat-strip .v{font-size:17px; font-weight:900; line-height:1;}
.stat-strip .k{font-size:9.5px; font-weight:800; letter-spacing:1px; color:var(--muted2);}
.stat-strip .d{font-size:9.5px; font-weight:800; color:#8ef0ac; opacity:0.9;}
.stat-strip .s.up .v{color:#8ef0ac;}

/* Raw PlayStyle icon art, verbatim from fut.gg -- no wrapper box, no
   label, no recoloring. Regular PlayStyles and PlayStyle+ (gold hexagon)
   both just place the real cropped icon at a consistent size. The
   max-width matters: without it an eight-column grid in a wide panel
   blows the icons up to a size they were never cropped for. */
.diamond-grid{display:grid; grid-template-columns:repeat(8, 1fr); gap:8px; width:100%; align-self:flex-start; margin-top:4px; align-items:center; justify-items:start;}
.ps-icon{width:100%; max-width:56px; height:auto; display:block;}
.fallback{font-size:20px; text-align:center;}

/* ---- SBC details: headline figure, meta tiles, challenge ladder ----
   Three boxed rows of equal weight made the panel read as a form. The
   money is the thing people scan for, so it gets the size; the two small
   facts pair off beneath it; and the challenges become a numbered ladder,
   which is what an SBC actually is. */
.headline{
  background:linear-gradient(135deg, rgba(255,43,43,0.13), rgba(255,43,43,0.02) 62%), var(--panel2);
  border:1px solid var(--line); border-left:3px solid var(--red);
  border-radius:12px; padding:14px 18px 15px;
}
.headline .k{font-size:10.5px; font-weight:800; letter-spacing:1.6px; color:var(--muted2); text-transform:uppercase;}
.headline .v{font-size:31px; font-weight:900; line-height:1.05; margin-top:6px; letter-spacing:-0.5px;}
.headline .v .u{font-size:14px; font-weight:800; color:var(--muted); letter-spacing:0; margin-left:6px;
  display:inline-flex; align-items:center; gap:5px; vertical-align:2px;}
/* The real EA currency marks, sized to the text they sit beside. */
.headline .v .u .cur{width:17px; height:17px; display:block;}
.headline .s .cur{width:14px; height:14px; display:inline-block; vertical-align:-3px; margin-right:5px;}
.headline .s{font-size:11.5px; color:var(--muted2); margin-top:5px; font-weight:700;}
.headline .s b{color:var(--muted); font-weight:800;}

/* Console and PC as two labelled halves rather than one number with the
   other trailing after it in prose: the platforms price differently, and
   running them together left it unclear which figure belonged to which.
   Each half owns its label, and the divider says they are alternatives,
   not parts of a sum. */
.price-split{display:flex; align-items:stretch; margin-top:9px;}
.price-split .side{flex:1; min-width:0; padding-right:14px;}
.price-split .side + .side{padding-right:0; padding-left:18px; border-left:1px solid var(--line);}
.price-split .p{
  display:flex; align-items:center; gap:6px;
  font-size:10px; font-weight:900; letter-spacing:1.6px; text-transform:uppercase; color:var(--muted2);
}
.price-split .p .dot{width:6px; height:6px; border-radius:50%; background:var(--red); flex-shrink:0; opacity:0.9;}
.price-split .side + .side .p .dot{background:var(--muted2); opacity:0.7;}
/* The currency's own mark, sized to whatever it sits beside. EVERY place
   a .cur can appear needs its own size rule -- an unsized one renders at
   the source image's full 240px and swallows the panel. */
.price-split .p .cur{width:18px; height:18px; flex-shrink:0; display:block;}
.price-split .u{display:flex; align-items:center; gap:5px;}
.price-split .u .cur{width:13px; height:13px; flex-shrink:0; display:block;}
/* Currency named inside ordinary text (see mark_currency). Sized in em so
   this single rule is right wherever the text lands -- a panel header, a
   task's payout chip, the unit under a price. */
.cur-inline{width:1.05em; height:1.05em; display:inline-block; vertical-align:-0.17em; margin-right:0.25em;}
.price-split .n{font-size:26px; font-weight:900; line-height:1.1; margin-top:3px; letter-spacing:-0.5px; white-space:nowrap;}
/* The cheaper platform stays full-strength; the dearer one is toned down
   a step so the better price is the one the eye lands on. */
.price-split .side.alt .n{color:var(--muted);}
.price-split .u{font-size:10.5px; font-weight:800; color:var(--muted2); letter-spacing:0.5px; margin-top:2px;}

.meta-strip{display:flex; gap:8px;}
.meta-tile{
  flex:1; background:var(--panel2); border:1px solid var(--line);
  border-radius:10px; padding:10px 14px;
}
.meta-tile .k{font-size:10px; font-weight:800; letter-spacing:1.4px; color:var(--muted2); text-transform:uppercase;}
.meta-tile .v{font-size:16px; font-weight:800; margin-top:4px;}

/* The ladder rail: one line behind the numbers, so eight challenges read
   as a sequence you climb rather than eight unrelated boxes. */
.ladder{position:relative; display:flex; flex-direction:column; gap:6px;}
.ladder::before{
  content:""; position:absolute; left:15px; top:14px; bottom:14px;
  width:2px; background:linear-gradient(180deg, var(--red2), var(--line));
  opacity:0.55;
}
.step{
  position:relative; display:flex; align-items:center; gap:12px;
  background:linear-gradient(90deg, rgba(255,43,43,0.055), rgba(255,43,43,0) 55%), var(--panel2);
  border:1px solid var(--line); border-radius:10px; padding:8px 14px 8px 8px;
}
.step .n{
  width:24px; height:24px; flex-shrink:0; border-radius:50%;
  background:var(--red-deep); border:1px solid var(--red2); color:var(--muted);
  display:flex; align-items:center; justify-content:center;
  font-size:11px; font-weight:900; z-index:1;
}
.step .kv{display:flex; align-items:center; justify-content:space-between; flex:1; gap:10px; min-width:0;}
.step .t{font-size:13px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
/* A challenge with several requirements stacks them under its name
   instead of trying to fit them all on one line -- a Marquee Matchups
   squad carries three, and running them together truncated the tail. */
.step.stacked{align-items:flex-start; padding-top:9px; padding-bottom:10px;}
.step.stacked .kv{flex-direction:column; align-items:flex-start; gap:6px;}
.step.stacked .t{white-space:normal;}
.step .reqs{display:flex; flex-wrap:wrap; gap:5px;}
.step .reqs .r{font-size:10.5px; padding:2px 9px;}
.step .r.more{color:var(--muted2); background:transparent; border-color:var(--line); border-style:dashed;}

/* An objective task: a name and a sentence, so the two stack on the left
   and the reward chip (when there is one) stays on the right. */
.step.task{align-items:flex-start; padding-top:9px; padding-bottom:10px;}
.step.task .kv{align-items:flex-start;}
.step.task .tw{min-width:0;}
.step.task .t{white-space:normal;}
.step.task .d{font-size:11.5px; color:var(--muted2); line-height:1.35; margin-top:3px;}
.step.task .r{align-self:center;}
.step .r{
  flex-shrink:0; font-size:11px; font-weight:800; color:#8ef0ac;
  background:rgba(61,220,127,0.07); border:1px solid rgba(61,220,127,0.22);
  border-radius:999px; padding:3px 10px;
}
/* The last challenge is the one that pays out, so it gets the accent. */
.step.final{border-color:var(--red2);}
.step.final .n{background:linear-gradient(180deg, #ff5c5c, var(--red)); border-color:#ff8080; color:#2a0606;}
"""


def row(icon: str, label: str, cond: str, accent: bool = False) -> str:
    cls = "row accent" if accent else "row"
    return (
        f'<div class="{cls}"><div class="icon">{icon}</div>'
        f'<div><div class="label">{label}</div><div class="cond">{cond}</div></div></div>'
    )


def compact_row(icon: str, label: str, value: str, cap: str = "") -> str:
    """One dense line for long lists: label left, value right, with an
    optional cap ("max 98") after the value. Used for an evolution's full
    upgrade list, which is too long for the boxed row style.

    An empty icon drops the icon element entirely rather than leaving an
    empty box: sixteen identical bullets down the side of an upgrade list
    are noise, not information."""
    cap_html = f' <span class="cap">{cap}</span>' if cap else ""
    icon_html = f'<div class="icon">{icon}</div>' if icon else ""
    return (
        f'<div class="row compact">{icon_html}'
        f'<div class="kv"><div class="label">{label}</div>'
        f'<div class="val">{value}{cap_html}</div></div></div>'
    )


def two_col(rows_html: str) -> str:
    """Lays a long run of compact rows out in two columns.

    An evolution can apply sixteen upgrades; as one column that is most of
    the card's height for a list nobody reads line by line."""
    return f'<div class="col2">{rows_html}</div>' if rows_html else ""


def headline_row(label: str, value: str, unit: str = "", sub: str = "") -> str:
    """The one figure the panel is really about, at a size you can read
    across a Discord feed. Everything smaller pairs off beneath it."""
    unit_html = f'<span class="u">{mark_currency(unit)}</span>' if unit else ""
    sub_html = f'<div class="s">{mark_currency(sub)}</div>' if sub else ""
    return (
        f'<div class="headline"><div class="k">{label}</div>'
        f'<div class="v">{value}{unit_html}</div>{sub_html}</div>'
    )


def reward_art(url: str) -> str:
    """Non-player reward artwork, with a gift-glyph fallback wired to the
    image's own onerror.

    fut.gg's pack and pick art are remote files; a 404, a rename or a
    blocked request would otherwise put a broken-image icon straight into
    a Discord post. The fallback is inert unless the load actually
    fails."""
    return (
        '<div class="art-wrap">'
        f'<img class="reward-art" src="{url}" '
        "onerror=\"this.style.display='none';"
        "this.nextElementSibling.style.display='flex';\">"
        '<div class="pack"><div class="glyph">🎁</div></div>'
        "</div>"
    )


def trade_badge(state: str) -> str:
    """"Tradeable" / "Untradeable" as a pill, or nothing when the payload
    doesn't say -- an unknown tradeability is worse guessed than omitted,
    since it's the difference between a reward you can sell and one you
    can't."""
    if not state:
        return ""
    cls = "trade yes" if state.lower() == "tradeable" else "trade"
    return f'<div class="{cls}">{state}</div>'


def alternatives_row(label: str, options: list[tuple[str, str]]) -> str:
    """Two ways of paying for the same thing, side by side.

    An evolution priced at 75,000 coins OR 250 FC Points is not a bill for
    both, so the two sit in their own labelled halves divided by a rule --
    the same shape the SBC card uses for console and PC, for the same
    reason. A single option falls back to the plain headline."""
    options = [(name, value) for name, value in options if value]
    if not options:
        return headline_row(label, "Unknown")
    if len(options) == 1:
        return headline_row(label, options[0][1], "", options[0][0])
    sides = ""
    for name, value in options:
        sides += (
            f'<div class="side"><div class="p">{_currency_mark(name)}{name}</div>'
            f'<div class="n">{value}</div></div>'
        )
    return (
        f'<div class="headline"><div class="k">{label}</div>'
        f'<div class="price-split">{sides}</div></div>'
    )


_CURRENCY_WORDS = {
    # Longest first below: "FC Points" must win over the bare "points"
    # inside it, so the mark is right rather than merely present.
    "fc points": "fc points", "fc point": "fc points",
    "coins": "coins", "coin": "coins",
    "points": "fc points", "point": "fc points",
}
_CURRENCY_WORD_RE = re.compile(
    r"\b(fc points|fc point|coins|coin|points|point)\b", re.IGNORECASE
)


def mark_currency(text: str) -> str:
    """Puts the currency's mark in front of the word wherever a string
    names one -- "25,000 coins", "250 FC Points".

    This is for currency mentioned inside ordinary text: a reward label, a
    task's payout chip, the unit under a price. A standalone currency
    label gets the larger dedicated mark from _currency_mark() instead.
    The icon is sized in em, so the single .cur-inline rule works at every
    text size it lands in.

    Marks the first mention of EACH currency -- "75,000 coins or 250 FC
    Points" names two things and both earn their mark -- but not repeats
    of the same one. Returns the text untouched when we have no icon for
    it, so nothing is ever lost by passing a string through here."""
    if not text:
        return text
    seen = set()

    def repl(m):
        key = _CURRENCY_WORDS.get(m.group(1).lower(), "")
        if key in seen:
            return m.group(0)
        uri = currency_icon_uri(key)
        if not uri:
            return m.group(0)
        seen.add(key)
        return f'<img class="cur cur-inline" src="{uri}" alt="">{m.group(0)}'

    return _CURRENCY_WORD_RE.sub(repl, text)


def _currency_mark(label: str, plain: bool = False) -> str:
    """The currency's icon for a standalone label like "Coins" or "FC
    Points" -- the labelled halves of a split price.

    plain=True returns nothing when we have no icon; otherwise it falls
    back to the plain dot, so an unrecognised currency still lines up with
    the ones we do know."""
    uri = currency_icon_uri(label)
    if uri:
        return f'<img class="cur" src="{uri}" alt="">'
    return "" if plain else '<span class="dot"></span>'


def evolved_stat_strip(base: dict, upgraded: dict) -> str:
    """The six face stats after the evolution, each showing what it came
    from when the evolution raised it.

    Returns "" when the upgraded player carries no stats, so the strip
    disappears rather than rendering six dashes."""
    after = extract_stats(upgraded)
    if not after:
        return ""
    before = extract_stats(base) or {}
    cells = ""
    for key, value in after.items():
        was = before.get(key)
        raised = isinstance(was, int) and isinstance(value, int) and value > was
        cls = "s up" if raised else "s"
        delta = f'<div class="d">from {was}</div>' if raised else ""
        cells += (
            f'<div class="{cls}"><div class="v">{value}</div>'
            f'<div class="k">{key}</div>{delta}</div>'
        )
    return f'<div class="stat-strip">{cells}</div>'


def price_split_row(label: str, prices: list[tuple[str, int]], unit: str = "coins") -> str:
    """A price per platform, each half owning its own label.

    prices is [(platform, amount), ...]. Identical amounts collapse to one
    half labelled with both platforms, since printing the same figure twice
    only invites the reader to look for a difference that isn't there. The
    dearer platform is toned down so the better price reads first."""
    if not prices:
        return headline_row(label, "Unknown")

    amounts = {amount for _, amount in prices}
    if len(amounts) == 1:
        platforms = " & ".join(p for p, _ in prices)
        return headline_row(label, f"{prices[0][1]:,}", unit, platforms)

    cheapest = min(amount for _, amount in prices)
    sides = ""
    for platform, amount in prices:
        cls = "side" if amount == cheapest else "side alt"
        sides += (
            f'<div class="{cls}"><div class="p"><span class="dot"></span>{platform}</div>'
            f'<div class="n">{amount:,}</div>'
            f'<div class="u">{mark_currency(unit)}</div></div>'
        )
    return (
        f'<div class="headline"><div class="k">{label}</div>'
        f'<div class="price-split">{sides}</div></div>'
    )


def meta_tiles(pairs: list[tuple[str, str]]) -> str:
    """Small facts side by side -- expiry, repeatable. Skips any pair with
    no value, and renders nothing at all when none are left, so the strip
    never appears as a row of empty boxes."""
    tiles = "".join(
        f'<div class="meta-tile"><div class="k">{k}</div>'
        f'<div class="v">{mark_currency(v)}</div></div>'
        for k, v in pairs if v
    )
    return f'<div class="meta-strip">{tiles}</div>' if tiles else ""


def task_ladder(steps: list[tuple[str, str, str]]) -> str:
    """An objective's tasks as a numbered ladder, in payload order.

    Each step is (title, description, reward). Shares the SBC ladder's
    rail, but an objective task is a sentence to read ("Score 3 goals in
    Live FUT Friendlies"), not a squad requirement to check off, so the
    description sits as a plain line under the name rather than being
    squeezed into a pill. The reward, when the payload names one, is a
    chip on the right.

    Unlike the SBC ladder, no step is accented: an objective's tasks all
    pay out, so singling out the last one would be a lie."""
    if not steps:
        return ""
    out = ""
    for i, (title, detail, reward) in enumerate(steps):
        chip = f'<div class="r">{mark_currency(reward)}</div>' if reward else ""
        line = f'<div class="d">{detail}</div>' if detail else ""
        out += (
            f'<div class="step task"><div class="n">{i + 1}</div>'
            f'<div class="kv"><div class="tw"><div class="t">{title}</div>{line}</div>'
            f"{chip}</div></div>"
        )
    return f'<div class="ladder">{out}</div>'


_MORE_CHIP = re.compile(r"^\+\d+ more$")


def challenge_ladder(steps: list[tuple[str, list[str]]]) -> str:
    """An SBC's challenges as a numbered ladder, in payload order.

    Each step is (title, [requirement, ...]). A real challenge often
    carries several ("MIN overall 77", "MAX 4 leagues", "MIN 22 total
    chem"), so the layout adapts: one requirement sits inline on the
    right, several stack under the title as their own chips. A challenge
    whose requirements we couldn't read shows its name alone rather than
    an empty chip.

    The last step carries the accent -- it's the one that pays out."""
    if not steps:
        return ""
    out = ""
    last = len(steps) - 1
    for i, (title, reqs) in enumerate(steps):
        reqs = [r for r in (reqs or []) if r]
        cls = "step final" if i == last else "step"
        if len(reqs) > 1:
            # "+2 more" is a count of what didn't fit, not a requirement,
            # so it doesn't get the requirement colour.
            chips = "".join(
                f'<div class="r more">{r}</div>' if _MORE_CHIP.match(r)
                else f'<div class="r">{r}</div>'
                for r in reqs
            )
            body = f'<div class="t">{title}</div><div class="reqs">{chips}</div>'
            cls += " stacked"
        else:
            chip = f'<div class="r">{reqs[0]}</div>' if reqs else ""
            body = f'<div class="t">{title}</div>{chip}'
        out += (
            f'<div class="{cls}"><div class="n">{i + 1}</div>'
            f'<div class="kv">{body}</div></div>'
        )
    return f'<div class="ladder">{out}</div>'


def panel(head_label: str, count_label: str, rows_html: str, centered: bool = False) -> str:
    body_cls = "panel-body centered" if centered else "panel-body"
    return (
        f'<div class="panel"><div class="panel-head"><span>{head_label}</span>'
        f'<span class="count">{mark_currency(count_label)}</span></div>'
        f'<div class="{body_cls}">{rows_html}</div></div>'
    )


def frame(
    game_label: str,
    category_label: str,
    title: str,
    sub: str,
    left_html: str,
    right_html: str,
    compact_reward: bool = False,
) -> str:
    """Builds the full card document. There is deliberately no footer bar:
    everything it used to carry (the title, the description, the expiry and
    the challenge/task count) is already shown above, so repeating it just
    made the card taller.

    compact_reward narrows the right-hand column. Pass it when the reward
    is a pack or a generic image rather than a player card -- there's
    nothing there worth half the card's width, and the detail column can
    use the room."""
    css = BASE_CSS.replace("LOGO_URI", logo_data_uri())
    left_cls = "col wide" if compact_reward else "col"
    right_cls = "col narrow" if compact_reward else "col"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{css}</style></head>
<body>
  <div class="glow"></div>
  <div class="watermark-logo"></div>
  <div class="frame">
    <header>
      <div class="tag">{category_label}</div>
    </header>

    <div class="eyebrow">{game_label} · ULTIMATE TEAM</div>
    <h1>{title}</h1>
    <p class="sub">{sub}</p>

    <div class="body">
      <div class="{left_cls}">{left_html}</div>
      <div class="{right_cls}">{right_html}</div>
    </div>
  </div>

  <div class="sitewatermark">
    <div class="brand-mini">
      <div class="mark"></div>
      <div class="name">FUT <span class="sol">SOLUTIONS</span></div>
    </div>
    <span class="dot">·</span>
    <span class="link">discord.gg/futsolutions</span>
  </div>
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


# ---------------------------------------------------------------------------
# Compositing evolved values onto the card artwork
# ---------------------------------------------------------------------------

STAT_BAND = (0.74, 0.88)          # vertical slice holding the six face stats
UPGRADE_GREEN = (45, 200, 105, 255)


def _find_stat_numbers(img):
    """Locates the six printed face-stat numbers on a card by finding the
    digit clusters in the stat band. Works whichever way round the card
    prints them (dark on a light card, light on a dark one) by measuring
    contrast against the band's own median colour."""
    import numpy as np
    from scipy import ndimage

    w, h = img.size
    a = np.array(img.convert("RGBA"))
    top, bot = int(h * STAT_BAND[0]), int(h * STAT_BAND[1])
    sub = a[top:bot, :, :3].astype(int)
    opaque = a[top:bot, :, 3] > 10
    if not opaque.any():
        return []
    med = np.median(sub[opaque], axis=0)
    dist = np.abs(sub - med).sum(axis=2)
    glyph = opaque & (dist > 150)

    lab, n = ndimage.label(glyph)
    boxes = []
    for i in range(1, n + 1):
        ys, xs = np.where(lab == i)
        if len(xs) < 15:
            continue
        bw, bh = xs.max() - xs.min(), ys.max() - ys.min()
        if bh < h * 0.02 or bh > h * 0.09 or bw > w * 0.12:
            continue
        boxes.append([xs.min(), xs.max(), ys.min() + top, ys.max() + top])
    if not boxes:
        return []

    boxes.sort(key=lambda b: b[0])
    groups, cur = [], [boxes[0]]
    for b in boxes[1:]:
        if b[0] - cur[-1][1] < w * 0.045:
            cur.append(b)
        else:
            groups.append(cur)
            cur = [b]
    groups.append(cur)
    nums = [
        (min(g[0] for g in grp), max(g[1] for g in grp),
         min(g[2] for g in grp), max(g[3] for g in grp))
        for grp in groups
    ]
    # Drop anything hugging the card edge (border ornaments).
    nums = [n_ for n_ in nums if n_[0] > w * 0.05 and n_[1] < w * 0.95]
    if len(nums) < 6:
        return []

    # The six values share a baseline; the stat LABELS above them are a
    # separate row and must not be mistaken for the numbers. Keep the
    # largest set of boxes whose vertical centres agree, then require
    # exactly six of them, evenly spaced.
    def even_six(row):
        if len(row) != 6:
            return False
        row = sorted(row, key=lambda n_: n_[0])
        gaps = [row[i + 1][0] - row[i][0] for i in range(5)]
        return max(gaps) - min(gaps) <= w * 0.06

    centres = sorted(set(round((n_[2] + n_[3]) / 2) for n_ in nums))
    qualifying = []
    for c in centres:
        row = [n_ for n_ in nums if abs((n_[2] + n_[3]) / 2 - c) <= h * 0.015]
        if even_six(row):
            qualifying.append((c, sorted(row, key=lambda n_: n_[0])))
    if not qualifying:
        return []
    # The stat LABELS (PAC, SHO, ...) form an equally valid six-column row
    # directly above the values, so take the lowest qualifying row.
    row = max(qualifying, key=lambda t: t[0])[1]

    # A column's label and its value sit close enough to merge into one
    # box. When that has happened the box spans both rows, so keep only its
    # lower half -- the value.
    tall = [n_ for n_ in row if (n_[3] - n_[2]) > h * 0.06]
    if len(tall) >= 4:
        row = [
            (x0, x1, int(y0 + (y1 - y0) * 0.52), y1) for (x0, x1, y0, y1) in row
        ]
    return row


def composite_evolved_card(image_bytes: bytes, base: dict, upgraded: dict):
    """Paints the evolution's resulting face stats into the card artwork,
    replacing the printed base numbers so the card reads as the evolved
    card. Stats the evolution doesn't raise keep the card's own number.

    Returns PNG bytes, or None if the card's numbers couldn't be located --
    in which case the caller should use the artwork untouched rather than
    drawing in the wrong place."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    up = extract_stats(upgraded)
    old = extract_stats(base) or {}
    if not up:
        return None
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    except Exception:
        return None

    spots = _find_stat_numbers(img)
    if not spots:
        return None

    out = img.copy()
    values = list(up.items())
    targets = []
    for (x0, x1, y0, y1), (key, value) in zip(spots, values):
        prev = old.get(key)
        if isinstance(prev, int) and isinstance(value, int) and value > prev:
            targets.append(((x0, x1, y0, y1), value))
    if not targets:
        return None

    # Erase each number by stretching the clean strip just above it over the
    # digits, so the patch keeps the card's own colour and texture.
    for (x0, x1, y0, y1), _ in targets:
        pad = max(2, (y1 - y0) // 6)
        bx0, bx1, by0, by1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
        band_h = by1 - by0
        donor = out.crop((bx0, max(by0 - band_h - 2, 0), bx1, max(by0 - 2, 1)))
        if donor.size[0] and donor.size[1]:
            donor = donor.resize((bx1 - bx0, band_h)).filter(ImageFilter.GaussianBlur(1.2))
            out.paste(donor, (bx0, by0))

    # The overall rating, same treatment: the biggest glyph cluster in the
    # card's upper-left quadrant.
    ovr_box = None
    up_ovr, base_ovr = upgraded.get("overall"), base.get("overall")
    if (PAINT_OVERALL and isinstance(up_ovr, int) and isinstance(base_ovr, int)
            and up_ovr != base_ovr):
        ovr_box = _find_overall(img)
        if ovr_box:
            x0, x1, y0, y1 = ovr_box
            pad = max(2, (y1 - y0) // 8)
            bx0, bx1, by0, by1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
            band_h = by1 - by0
            donor = out.crop((bx0, max(by0 - band_h - 2, 0), bx1, max(by0 - 2, 1)))
            if donor.size[0] and donor.size[1]:
                donor = donor.resize((bx1 - bx0, band_h)).filter(ImageFilter.GaussianBlur(1.2))
                out.paste(donor, (bx0, by0))

    draw = ImageDraw.Draw(out)
    size = max(10, int((targets[0][0][3] - targets[0][0][2]) * 1.2))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf", size
        )
    except Exception:
        font = ImageFont.load_default()
    for (x0, x1, y0, y1), value in targets:
        text = str(value)
        tb = draw.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        draw.text((cx - tw / 2 - tb[0], cy - th / 2 - tb[1]), text,
                  font=font, fill=UPGRADE_GREEN)

    if ovr_box:
        x0, x1, y0, y1 = ovr_box
        ovr_font_size = max(12, int((y1 - y0) * 1.15))
        try:
            ovr_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf", ovr_font_size
            )
        except Exception:
            ovr_font = font
        text = str(up_ovr)
        tb = draw.textbbox((0, 0), text, font=ovr_font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        draw.text((cx - tw / 2 - tb[0], cy - th / 2 - tb[1]), text,
                  font=ovr_font, fill=UPGRADE_GREEN)

    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# Repainting the overall rating is DISABLED. Two detection attempts both
# misfired on a real card -- one anchored on the crest above the rating,
# the other caught a single digit of a two-digit number and painted it
# offset. On an unattended feed a misplaced number is worse than an
# unchanged one, so the rating is left as the card prints it and the
# "Overall Rating +20" row in the upgrades list carries that information
# instead. The detection below is kept for whoever picks this up next.
PAINT_OVERALL = False


def _find_overall(img):
    """Locates the printed overall rating: the largest glyph cluster in the
    card's upper-left quadrant, where every FC card template puts it.
    Returns None if nothing convincing is there, so the caller leaves the
    rating alone rather than painting over the artwork.

    NOT RELIABLE YET -- see PAINT_OVERALL above."""
    import numpy as np
    from scipy import ndimage

    w, h = img.size
    a = np.array(img.convert("RGBA"))
    x0, x1 = int(w * 0.04), int(w * 0.45)
    y0, y1 = int(h * 0.05), int(h * 0.32)
    sub = a[y0:y1, x0:x1, :3].astype(int)
    opaque = a[y0:y1, x0:x1, 3] > 10
    if not opaque.any():
        return None
    med = np.median(sub[opaque], axis=0)
    glyph = opaque & (np.abs(sub - med).sum(axis=2) > 150)
    lab, n = ndimage.label(glyph)
    boxes = []
    for i in range(1, n + 1):
        ys, xs = np.where(lab == i)
        bh = ys.max() - ys.min()
        if bh < h * 0.035 or bh > h * 0.16:
            continue
        boxes.append([xs.min(), xs.max(), ys.min(), ys.max()])
    if not boxes:
        return None
    # The rating is the largest text on the card, so anchor on the tallest
    # glyph rather than the topmost -- crests and ornaments sit above it.
    tallest = max(boxes, key=lambda b: b[3] - b[2])
    ref_h = tallest[3] - tallest[2]
    row = [
        b for b in boxes
        if abs(b[2] - tallest[2]) <= h * 0.02 and (b[3] - b[2]) > ref_h * 0.6
    ]
    if not row:
        return None
    return (min(b[0] for b in row) + x0, max(b[1] for b in row) + x0,
            min(b[2] for b in row) + y0, max(b[3] for b in row) + y0)


# ---------------------------------------------------------------------------
# Compact evolution layout
# ---------------------------------------------------------------------------

EVO_CSS = """
:root{
  --bg:#0a0605; --panel:#160c0c; --panel2:#1c0f0f; --line:#3a1414;
  --ink:#f6efee; --muted:#c99; --muted2:#a67373;
  --red:#ff2b2b; --up:#3ddc7f;
}
*{box-sizing:border-box; margin:0; padding:0;}
body{width:1000px; background:var(--bg); color:var(--ink);
  font-family:Arial, "Helvetica Neue", sans-serif; position:relative;}
.bg{position:absolute; inset:0;
  background:radial-gradient(720px 380px at 50% -160px, rgba(255,43,43,0.18), rgba(255,43,43,0) 70%), var(--bg);}
.watermark-logo{position:absolute; left:50%; top:52%; transform:translate(-50%,-50%);
  width:340px; height:340px; background-image:url('LOGO_URI'); background-size:contain;
  background-repeat:no-repeat; background-position:center; opacity:0.03;}
.wrap{position:relative; z-index:2; padding:22px 26px 18px;}

.top{display:flex; align-items:center; gap:11px; padding-bottom:14px;}
.top .mark{width:24px; height:24px; background-image:url('LOGO_URI'); background-size:contain;
  background-repeat:no-repeat; background-position:center;}
.top .name{font-size:12.5px; font-weight:900; letter-spacing:1.6px;}
.top .name span{color:var(--red);}
.top .spacer{flex:1;}
.top .game{font-size:10.5px; font-weight:700; letter-spacing:2.2px; color:var(--muted2);}
.tag{background:var(--red); color:#1a0303; font-weight:900; font-size:12px;
  letter-spacing:1.8px; padding:6px 14px; border-radius:6px;}

.head{margin-bottom:12px;}
.head .title{font-size:26px; font-weight:900; letter-spacing:0.2px; line-height:1.1;}
.head .desc{font-size:12.5px; color:var(--muted); margin-top:5px; max-width:760px; line-height:1.45;}

/* Cost and timings live in their own box on the right of the detail row,
   filling space the upgrade list doesn't need. */
.cost-box .amount{font-size:20px; font-weight:900; line-height:1.2;}
.cost-box .or{font-size:11px; font-weight:700; color:var(--muted2); letter-spacing:1px; margin:1px 0;}
.cost-box .how{font-size:12px; color:var(--up); line-height:1.45; margin-top:5px; font-weight:700;}
.cost-box .t{font-size:12px; color:var(--muted); line-height:1.75; margin-top:9px;}
.cost-box .t b{color:var(--ink); font-weight:700;}
.cost-box .rep{margin-top:9px; display:inline-block; font-size:10.5px; font-weight:800;
  letter-spacing:1.2px; color:var(--muted2); border:1px solid var(--line);
  border-radius:5px; padding:3px 9px;}

/* Player strip: who the evo is for and what it makes them. */
.player{display:flex; align-items:center; gap:18px; background:var(--panel);
  border:1px solid var(--line); border-radius:12px; padding:12px 18px; margin-bottom:12px;}
.player .who{min-width:190px;}
.player .pname{font-size:17px; font-weight:900; line-height:1.15;}
.player .ppos{font-size:11px; color:var(--muted2); margin-top:3px; letter-spacing:1px;}
.player .ovr{display:flex; align-items:baseline; gap:6px; padding-right:18px; border-right:1px solid var(--line);}
.player .ovr .was{font-size:16px; font-weight:800; color:var(--muted2);}
.player .ovr .ar{font-size:12px; color:var(--red); font-weight:900;}
.player .ovr .now{font-size:26px; font-weight:900; color:var(--up); line-height:1;}
.player .ovr .u{font-size:9.5px; font-weight:800; color:var(--muted2); letter-spacing:1px;}
.stats{display:flex; gap:16px; flex:1; justify-content:space-around;}
.stats .s{text-align:center;}
.stats .s .k{font-size:9.5px; font-weight:800; letter-spacing:1px; color:var(--muted2);}
.stats .s .val{font-size:15px; font-weight:900; margin-top:2px;}
.stats .s .val .old{font-size:11px; font-weight:700; color:var(--muted2);}
.stats .s.up .val{color:var(--up);}

.cols{display:flex; gap:12px; align-items:stretch;}
.box{background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 16px;}
.box h2{font-size:11px; font-weight:900; letter-spacing:1.8px; color:var(--red);
  text-transform:uppercase; margin-bottom:7px;}
/* Second heading inside the requirements box: eligibility above, the
   challenges you play below. */
.box h2.sub{margin-top:11px; color:var(--muted2);}
.col-a{width:212px; display:flex; flex-direction:column; gap:12px;}
/* An unused column takes no space, so a card with nothing for it gives
   that width to the columns that do have content. */
.col-a:empty, .col-c:empty{display:none;}
.col-b{flex:1;}
.col-c{width:212px;}
.line{font-size:12.5px; line-height:1.65; color:#ded2d2;}
.line .v{color:var(--up); font-weight:800;}
.line .cap{color:var(--muted2); font-size:11px;}
.grid{columns:2; column-gap:22px;}
.group{font-size:10px; font-weight:800; letter-spacing:1.3px; text-transform:uppercase;
  color:var(--muted2); margin-top:8px; break-after:avoid;}
.group:first-child{margin-top:0;}

/* PlayStyles and Roles sit side by side; either one drops out cleanly if
   its data isn't there. */
.ps-row{display:flex; gap:12px; margin-top:12px; align-items:stretch;}
.ps{flex:1; background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:11px 16px;}
.ps h2{font-size:11px; font-weight:900; letter-spacing:1.8px; color:var(--red);
  text-transform:uppercase; margin-bottom:8px;}
.roles{display:flex; flex-wrap:wrap; gap:6px;}
.role{background:var(--panel2); border:1px solid var(--line); border-radius:6px;
  padding:4px 10px; font-size:11px; font-weight:700;}
.role b{color:var(--up); font-weight:900;}
/* Marked when this evolution adds the role or raises its familiarity. */
.role.gained{border-color:var(--up); color:var(--ink);}
.brand{text-align:center; font-size:10.5px; color:var(--muted2); letter-spacing:1px; padding-top:12px;}
"""


def evo_stat_cell(key: str, before, after) -> str:
    """One face stat in the player strip: the evolved value, with the value
    it came from beside it and the pair marked when the evo raises it."""
    raised = isinstance(before, int) and isinstance(after, int) and after > before
    cls = "s up" if raised else "s"
    old = f'<span class="old">{before} &rsaquo; </span>' if raised else ""
    shown = after if after is not None else (before if before is not None else "-")
    return f'<div class="{cls}"><div class="k">{key}</div><div class="val">{old}{shown}</div></div>'


def compact_frame(kind_top: str, kind_bottom: str, game_label: str,
                  title: str, description: str, player_html: str,
                  col_a_html: str, col_b_html: str, col_c_html: str,
                  bottom_html: str = "") -> str:
    """The compact card layout shared by evolutions and SBCs: a header, the
    title and description, an optional player strip, a three-column detail
    row, and an optional row of boxes underneath. Callers decide what goes
    in each column."""
    css = EVO_CSS.replace("LOGO_URI", logo_data_uri())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{css}</style></head>
<body>
  <div class="bg"></div>
  <div class="watermark-logo"></div>
  <div class="wrap">
    <div class="top">
      <div class="mark"></div>
      <div class="name">FUT <span>SOLUTIONS</span></div>
      <div class="spacer"></div>
      <div class="game">{game_label} &middot; ULTIMATE TEAM</div>
      <div class="tag">{kind_top} {kind_bottom}</div>
    </div>

    <div class="head">
      <div class="title">{title}</div>
      <div class="desc">{description}</div>
    </div>

    {player_html}

    <div class="cols">
      <div class="col-a">{col_a_html}</div>
      <div class="col-b">{col_b_html}</div>
      <div class="col-c">{col_c_html}</div>
    </div>

    {bottom_html}
    <div class="brand">discord.gg/futsolutions</div>
  </div>
</body></html>"""


def evolution_frame(kind_top: str, kind_bottom: str, game_label: str,
                    requirement_lines: str, award_groups: str,
                    player_html: str, roles_html: str,
                    title: str, description: str,
                    cost: str, timing_html: str) -> str:
    """An evolution laid out in the shared compact frame."""
    return compact_frame(
        kind_top, kind_bottom, game_label, title, description, player_html,
        col_a_html=f'<div class="box"><h2>Requirements</h2>{requirement_lines}</div>',
        col_b_html=(f'<div class="box"><h2>Upgrades Applied</h2>'
                    f'<div class="grid">{award_groups}</div></div>'),
        col_c_html=(f'<div class="box cost-box"><h2>Cost &amp; Timing</h2>'
                    f'{cost}{timing_html}</div>'),
        bottom_html=roles_html,
    )


# FC's role names, by the position that uses them. Roles are a separate
# system from PlayStyles: familiarity is written with "+" and "++", and an
# evolution can raise it. Matching against this list means role data can be
# found wherever fut.gg puts it, for the same reason the PlayStyle
# vocabulary works -- and it can't swallow unrelated strings.
ROLE_NAMES = {
    # goalkeeper
    "goalkeeper", "sweeper keeper",
    # defenders
    "fullback", "falseback", "wingback", "offensive wingback",
    "inverted wingback", "defender", "stopper", "ball playing defender",
    # midfield
    "holding", "centre half", "center half", "deep lying playmaker",
    "wide half", "box crasher", "box to box", "playmaker", "half winger",
    "holding midfielder",
    # wide / attack
    "winger", "inside forward", "wide playmaker", "classic winger",
    "advance forward", "advanced forward", "poacher", "false 9",
    "target forward", "shadow striker",
}


def _role_slug(name: str) -> str:
    return name.strip().lower().rstrip("+").strip()


def extract_roles(player: dict) -> list[str]:
    """Finds the player's roles anywhere in their payload, returning names
    with their familiarity suffix ("Holding++"). Same vocabulary-driven walk
    as extract_playstyle_names(): the field name doesn't matter, and only
    values that are actually role names are kept.

    Returns [] when the payload carries no roles -- which is the expected
    result if fut.gg doesn't expose them, and simply omits the section."""
    if not player:
        return []

    found: dict[str, str] = {}
    order: list[str] = []

    def note(name: str, suffix: str):
        if name not in found:
            order.append(name)
            found[name] = suffix
        elif len(suffix) > len(found[name]):
            found[name] = suffix          # keep the highest familiarity seen

    def match(value):
        if not isinstance(value, str) or not value:
            return None
        if len(value) > MAX_PLAYSTYLE_NAME_LEN or any(c in value for c in "/:;{}<>\n"):
            return None
        slug = _role_slug(value)
        return slug if slug in ROLE_NAMES else None

    def suffix_for(value: str, path_plus: int) -> str:
        # Role familiarity has two levels, + and ++ -- there is no bare
        # role. Anything we find without an explicit level is therefore at
        # least "+", never suffix-less.
        stripped = value.strip()
        if stripped.endswith("++"):
            return "++"
        if stripped.endswith("+"):
            return "+"
        return "+" * max(path_plus, 1)

    def walk(node, path_plus: int):
        if isinstance(node, dict):
            level = node.get("familiarity") or node.get("level") or node.get("tier")
            name_val = node.get("name") or node.get("role") or node.get("label")
            slug = match(name_val)
            if slug:
                if isinstance(level, int) and level > 0:
                    note(slug, "+" * max(min(level, 2), 1))
                else:
                    note(slug, suffix_for(str(name_val), path_plus))
                return
            for key, value in node.items():
                k = str(key).lower()
                walk(value, 2 if "plusplus" in k or "++" in k else
                     (1 if "plus" in k else path_plus))
        elif isinstance(node, list):
            for entry in node:
                walk(entry, path_plus)
        else:
            slug = match(node)
            if slug:
                note(slug, suffix_for(str(node), path_plus))

    walk(player, 0)
    return [s.title() + found[s] for s in order]


def role_chips(names: list[str], base_names: list[str] | None = None) -> str:
    """Roles as chips, highest familiarity first.

    When the pre-evolution roles are given, anything this evolution adds or
    raises is marked, so the card shows what the evo actually grants rather
    than just the finished player's roles. That comparison is per item, so
    it holds for whatever a future evolution happens to change."""
    if not names:
        return ""
    before = {}
    for b in (base_names or []):
        before[b.rstrip("+").lower()] = b.count("+")

    ordered = sorted(names, key=lambda n: -n.count("+"))
    chips = ""
    for n in ordered:
        label, plus = n.rstrip("+"), n.count("+")
        was = before.get(label.lower())
        gained = base_names is not None and (was is None or plus > was)
        cls = "role gained" if gained else "role"
        chips += f'<div class="{cls}">{label}<b>{"+" * plus}</b></div>'
    return chips


# ---------------------------------------------------------------------------
# Evolution challenges ("how to unlock")
# ---------------------------------------------------------------------------

# Keys that plausibly hold an evolution's challenges. An evolution's
# eligibility (requirementsText) is a different thing and is shown
# separately -- these are the tasks you actually play to complete it.
_CHALLENGE_KEY_HINTS = ("challenge", "task", "objective", "level", "stage", "tier")


def extract_evo_challenges(evo: dict) -> list[dict]:
    """Finds an evolution's challenges -- the things you have to do to
    complete it, as opposed to the entry requirements saying who is
    eligible.

    fut.gg's field name for these isn't something to rely on, so this looks
    for challenge-shaped lists (dicts carrying a name/description) sitting
    under a key that reads like challenges, levels or tasks. Requirement
    and upgrade lists are explicitly skipped so eligibility can't be
    mistaken for a challenge.

    Returns [] when nothing convincing is found, and the caller omits the
    section rather than showing a guess."""
    if not evo:
        return []

    found: list[dict] = []

    def looks_like_challenges(value) -> bool:
        if not isinstance(value, list) or not value:
            return False
        entries = [e for e in value if isinstance(e, dict)]
        if len(entries) != len(value):
            return False
        return all(
            any(isinstance(e.get(k), str) and e.get(k).strip()
                for k in ("name", "title", "description", "text"))
            for e in entries
        )

    def walk(node, key_hinted: bool):
        if isinstance(node, dict):
            for key, value in node.items():
                k = str(key).lower()
                if "requirement" in k or "upgrade" in k:
                    continue                    # eligibility / rewards, not challenges
                hinted = key_hinted or any(h in k for h in _CHALLENGE_KEY_HINTS)
                if hinted and looks_like_challenges(value):
                    found.extend(e for e in value if isinstance(e, dict))
                else:
                    walk(value, hinted)
        elif isinstance(node, list):
            for entry in node:
                walk(entry, key_hinted)

    walk(evo, False)
    return found


def challenge_lines(challenges: list[dict]) -> str:
    """Renders challenges as plain lines, numbered when there is more than
    one -- an evolution's levels are ordered and people work through them."""
    if not challenges:
        return ""
    out = ""
    for i, c in enumerate(challenges, 1):
        title = ""
        for key in ("name", "title"):
            if isinstance(c.get(key), str) and c[key].strip():
                title = c[key].strip()
                break
        detail = ""
        for key in ("description", "text", "requirement"):
            if isinstance(c.get(key), str) and c[key].strip() and c[key].strip() != title:
                detail = c[key].strip()
                break
        if not title:
            title, detail = detail, ""
        if not title:
            continue
        num = f"{i}. " if len(challenges) > 1 else ""
        tail = f' <span class="cap">{detail}</span>' if detail else ""
        out += f'<div class="line">{num}{title}{tail}</div>'
    return out


# Fields that plausibly describe how an evolution is obtained when it
# isn't simply bought with coins or FC Points -- e.g. earned from an
# objective, unlocked by a pack, or granted by a campaign.
_ACQUISITION_KEY_HINTS = (
    "unlockmethod", "unlocktype", "acquisition", "obtained", "obtainedfrom",
    "source", "availability", "howtoget", "unlockedby", "acquiredfrom",
)


def extract_acquisition(evo: dict) -> str:
    """Returns a short description of how an evolution is obtained, when
    the payload carries one. Only used when there is no coin/point/token
    price -- a priceless evolution isn't necessarily free, it may be earned
    some other way, and saying "FREE" for one of those is the kind of wrong
    that costs somebody time."""
    if not evo:
        return ""

    best = ""

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            for key, value in node.items():
                k = str(key).lower().replace("_", "")
                if any(h in k for h in _ACQUISITION_KEY_HINTS):
                    if isinstance(value, str) and value.strip() and not best:
                        best = value.strip()
                    elif isinstance(value, dict):
                        for cand in ("name", "label", "description", "text"):
                            v = value.get(cand)
                            if isinstance(v, str) and v.strip() and not best:
                                best = v.strip()
                                break
                walk(value)
        elif isinstance(node, list):
            for entry in node:
                walk(entry)

    walk(evo)
    return best
