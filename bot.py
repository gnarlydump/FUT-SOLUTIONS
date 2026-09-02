"""
fut.gg -> Discord notifier

Checks fut.gg for newly-added Evolutions, SBCs, and Objectives, and posts
each to its own Discord webhook. Designed to run on a schedule (see
.github/workflows/check.yml) via GitHub Actions, but works fine run locally
too.

How it gets data:
  fut.gg is a client-rendered app (TanStack Start) that embeds its page data
  in a global `window.__TSR_ROUTER__` object once loaded. There's no public
  JSON API for most pages, so this script uses Playwright (headless
  Chromium) to load each page for real and pull the data out of that object
  directly -- the exact same data structure the site itself renders from.
  SBCs are the one exception: fut.gg moved that page to a paginated
  client-side API (see SBC_API below), so we call that endpoint directly
  instead.

State:
  Previously-seen ids for each category are stored in state/state.json. On
  the very first run (no ids recorded yet for a category), the script seeds
  the file with everything currently live WITHOUT posting -- otherwise
  you'd get 200+ messages dumped into your channel on the first run. Every
  run after that only posts genuinely new items.

Rate limiting:
  Discord webhooks reject requests sent too fast (~5 per 2 seconds). Posts
  are spaced out with a short delay, and a 429 (rate limited) response is
  retried automatically rather than treated as a failure.

Role pings:
  Setting EVOLUTIONS_ROLE_ID / SBC_ROLE_ID / OBJECTIVES_ROLE_ID pings that
  role at the start of the announcement message (e.g. so your "New SBC"
  reaction-role members get notified). Leave any of them unset to post
  without a ping for that category.
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

import cards

FUTGG_BASE = "https://www.fut.gg"
EVOLUTIONS_URL = f"{FUTGG_BASE}/evolutions/"
SBC_URL = f"{FUTGG_BASE}/sbc/"
OBJECTIVES_URL = f"{FUTGG_BASE}/objectives/"
# fut.gg's SBC list page used to embed every SBC in the page's initial
# loaderData (loaderData.allSbcs). fut.gg restructured that page at some
# point to only load category summaries up front and fetch the actual SBC
# sets client-side (via React Query) from this paginated endpoint instead.
# {page} is 1-indexed; the response has {data: [...], next, totalPages}.
SBC_API = f"{FUTGG_BASE}/api/fut/sbc/"

STATE_PATH = Path(__file__).parent / "state" / "state.json"

EVOLUTIONS_WEBHOOK_URL = os.environ.get("EVOLUTIONS_WEBHOOK_URL", "")
SBC_WEBHOOK_URL = os.environ.get("SBC_WEBHOOK_URL", "")
OBJECTIVES_WEBHOOK_URL = os.environ.get("OBJECTIVES_WEBHOOK_URL", "")
EXPIRING_EVOLUTIONS_WEBHOOK_URL = os.environ.get("EXPIRING_EVOLUTIONS_WEBHOOK_URL", "")

# Optional: Discord role IDs to @-mention when posting. If left blank, the
# post still goes out, just without a role ping. These correspond to the
# "New Evolution" / "New SBC" / "New Objective" / "Evolution Expiring"
# reaction roles.
EVOLUTIONS_ROLE_ID = os.environ.get("EVOLUTIONS_ROLE_ID", "")
SBC_ROLE_ID = os.environ.get("SBC_ROLE_ID", "")
OBJECTIVES_ROLE_ID = os.environ.get("OBJECTIVES_ROLE_ID", "")
EXPIRING_ROLE_ID = os.environ.get("EXPIRING_ROLE_ID", "")

EMBED_COLOR_EVOLUTION = 0x5865F2  # discord blurple
EMBED_COLOR_SBC = 0x57F287  # green
EMBED_COLOR_OBJECTIVE = 0xFEE75C  # yellow
EMBED_COLOR_EXPIRING = 0xED4245  # red -- urgency

# Expiring-evolution reminder stages, ordered furthest-out first. Each is
# (stage_name, hours_before_expiry). Every stage whose window has been
# entered and hasn't already been posted (tracked per-evolution in
# state["evolutions_expiry_notified"]) gets its own post -- so an evolution
# discovered with 40 hours left gets only the 6h reminder later, while one
# discovered with 60 hours left gets both the 48h warning and the 6h final
# reminder as time passes.
EXPIRY_REMINDER_STAGES = [
    ("48h", 48),
    ("final", 6),
]

# Discord webhooks are rate-limited (~5 requests per 2 seconds). Posting a
# batch of new items back-to-back with no pause can trip that limit and
# Discord will reject the message. This is the pause between each post.
POST_DELAY_SECONDS = 1.5

# Ceiling on BACKFILL_COUNT. A backfill posts straight into live community
# channels, so the cap is what stops a mistyped value flooding them.
MAX_BACKFILL = 25

# Bump this whenever the cards change enough to be worth showing off. The
# first run on a version the state hasn't recorded republishes the newest
# BACKFILL_ON_UPDATE items of each category, so the redesign lands in the
# channels by itself rather than waiting for enough new content to appear.
CARD_DESIGN_VERSION = 2
BACKFILL_ON_UPDATE = 5

DEFAULT_STATE = {
    "evolutions_seen": [],
    "sbcs_seen": [],
    "objectives_seen": [],
    # Maps evolution id (as a string, since JSON object keys must be
    # strings) -> list of reminder stage names already posted for it, e.g.
    # {"1234": ["48h"]}. Prevents re-posting the same reminder every hour.
    "evolutions_expiry_notified": {},
    # The card design the channels have already seen. When this doesn't
    # match CARD_DESIGN_VERSION the next run republishes a few recent
    # items so the channels show the new design without anyone having to
    # trigger anything. 0 means "never recorded", which is the state every
    # existing deployment is in.
    "card_design_version": 0,
}


# Set by main() when the state's recorded card design is out of date. Env
# BACKFILL_COUNT still wins, so a manual run can ask for a different size.
_AUTO_BACKFILL = 0


def backfill_count() -> int:
    """How many items per category this run should repost regardless of
    what's already been posted, or 0 for normal behaviour.

    Comes from BACKFILL_COUNT for a deliberate manual run, or from the
    automatic one-shot after a card redesign (see CARD_DESIGN_VERSION).
    Capped either way: this posts to live community channels, so a typo
    shouldn't be able to dump hundreds of messages into them."""
    raw = (os.environ.get("BACKFILL_COUNT") or "").strip()
    if not raw:
        return min(_AUTO_BACKFILL, MAX_BACKFILL)
    try:
        n = int(raw)
    except ValueError:
        print(f"  ! BACKFILL_COUNT={raw!r} is not a number -- ignoring.")
        return 0
    if n <= 0:
        return 0
    if n > MAX_BACKFILL:
        print(f"  ! BACKFILL_COUNT={n} exceeds the {MAX_BACKFILL} cap -- using {MAX_BACKFILL}.")
        return MAX_BACKFILL
    return n


def role_mention(role_id: str) -> str:
    """Returns a Discord role-mention prefix (with trailing space) if a role
    id is configured, otherwise an empty string so the post still goes out
    without a ping."""
    return f"<@&{role_id}> " if role_id else ""


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def load_state() -> dict:
    state = dict(DEFAULT_STATE)
    if STATE_PATH.exists():
        state.update(json.loads(STATE_PATH.read_text()))
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Scraping (via headless browser -- see module docstring for why)
# ---------------------------------------------------------------------------

def goto_and_wait_for_router(page, url: str, ready_check_js: str) -> None:
    """Navigate to a fut.gg page and wait until the SPECIFIC piece of router
    state we're about to read is actually populated -- NOT until the network
    goes fully idle, and NOT just until *some* router match exists.

    fut.gg pages carry ad-network/analytics scripts (AdThrive etc.) that keep
    making background requests indefinitely, so `wait_until="networkidle"`
    can hang for the full 30s timeout and kill the whole run even though the
    page data we actually need (window.__TSR_ROUTER__) was ready in a couple
    seconds. But waiting only for "any router match to exist" is too loose --
    TanStack Start resolves matches in stages, so an early, incomplete match
    can appear before the one carrying our actual data (loaderData.evolutions
    etc.), and evaluating too early silently returns an empty list instead of
    erroring. `ready_check_js` is a JS boolean expression checking for the
    exact data each caller is about to read, so we only proceed once it's
    genuinely there.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_function(ready_check_js, timeout=20000)


def fetch_evolutions(page) -> list[dict]:
    # Match by the SHAPE of loaderData (which match has an
    # `evolutions.data` array), not by the route's id string. fut.gg has
    # changed that id string more than once (e.g. '/evolutions/' became
    # '/evolutions//evolutions/' after a router upgrade) with no notice --
    # matching by id broke silently (empty result, no error) each time.
    # Matching by data shape survives those renames.
    goto_and_wait_for_router(
        page,
        EVOLUTIONS_URL,
        """
        () => {
            const matches = window.__TSR_ROUTER__ && window.__TSR_ROUTER__.state.matches;
            return !!(matches && matches.some(
                m => m.loaderData && m.loaderData.evolutions && m.loaderData.evolutions.data
            ));
        }
        """,
    )
    data = page.evaluate(
        """
        () => {
            const m = window.__TSR_ROUTER__.state.matches.find(
                m => m.loaderData && m.loaderData.evolutions && m.loaderData.evolutions.data
            );
            return m ? m.loaderData.evolutions.data : [];
        }
        """
    )
    return data or []


def fetch_sbcs(page) -> list[dict]:
    """fut.gg's SBC list page no longer embeds all SBCs in its initial
    loaderData (that used to live at loaderData.allSbcs) -- it now only
    loads category summaries up front and fetches the actual SBC sets
    client-side, paginated, from SBC_API. So we load the page (mainly to
    get a same-origin context to fetch from) and then page through that
    API directly, same as fut.gg's own frontend does."""
    goto_and_wait_for_router(
        page,
        SBC_URL,
        "() => window.__TSR_ROUTER__ && window.__TSR_ROUTER__.state.matches.length > 0",
    )
    result = page.evaluate(
        """
        async (apiUrl) => {
            const all = [];
            const debug = [];
            let pageNum = 1;
            for (let i = 0; i < 20; i++) {
                let r;
                try {
                    r = await fetch(`${apiUrl}?page=${pageNum}`, {
                        headers: { Accept: 'application/json' },
                    });
                } catch (e) {
                    debug.push(`page ${pageNum}: network error ${String(e)}`);
                    break;
                }
                if (!r.ok) {
                    let bodySnippet = '';
                    try { bodySnippet = (await r.text()).slice(0, 200); } catch (e) {}
                    debug.push(`page ${pageNum}: HTTP ${r.status} ${bodySnippet}`);
                    break;
                }
                const json = await r.json();
                debug.push(`page ${pageNum}: got ${(json.data || []).length} item(s), next=${json.next}`);
                all.push(...(json.data || []));
                if (!json.next) break;
                pageNum = json.next;
            }
            return { items: all, debug };
        }
        """,
        SBC_API,
    )
    items = (result or {}).get("items") or []
    debug_lines = (result or {}).get("debug") or []
    if not items:
        print("  ! SBC API returned no items -- diagnostic trace:")
        for line in debug_lines:
            print(f"    {line}")
    return items


def fetch_objectives(page) -> list[dict]:
    # Same reasoning as fetch_evolutions() -- match by data shape
    # (loaderData.allObjectives), not the route id string, which fut.gg has
    # also renamed (e.g. gained a trailing '/objectives' segment) without
    # notice.
    goto_and_wait_for_router(
        page,
        OBJECTIVES_URL,
        """
        () => {
            const matches = window.__TSR_ROUTER__ && window.__TSR_ROUTER__.state.matches;
            return !!(matches && matches.some(m => m.loaderData && m.loaderData.allObjectives));
        }
        """,
    )
    data = page.evaluate(
        """
        () => {
            const m = window.__TSR_ROUTER__.state.matches.find(
                m => m.loaderData && m.loaderData.allObjectives
            );
            return m ? m.loaderData.allObjectives : [];
        }
        """
    )
    return data or []


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def player_name(p: dict) -> str:
    if p.get("nickname"):
        return p["nickname"]
    return f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()


def truncate(text: str, limit: int) -> str:
    """Truncates at the last word boundary within `limit` chars (instead of
    slicing mid-word) and appends an ellipsis when anything was cut."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(".,;: ")
    return (cut or text[:limit]) + "…"


def format_kv_value(item: dict) -> str:
    """Same value formatting as format_kv_lines(), for a single
    requirement/upgrade row rendered into a card instead of Discord
    markdown."""
    value = item.get("value", "")
    max_value = item.get("maxValue")
    return f"{value} {max_value}" if max_value else str(value)


def format_kv_lines(items: list[dict], limit: int = 12) -> str:
    """requirementsText / totalUpgradesText are lists of {label, value[, maxValue]}."""
    lines = []
    for item in items[:limit]:
        label = item.get("label", "")
        value = item.get("value", "")
        max_value = item.get("maxValue")
        if max_value:
            lines.append(f"**{label}:** {value} {max_value}")
        else:
            lines.append(f"**{label}:** {value}")
    if len(items) > limit:
        lines.append(f"...and {len(items) - limit} more")
    return "\n".join(lines) if lines else "None"


def relative_days(iso_ts: str) -> str:
    if not iso_ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return iso_ts
    delta = dt - datetime.now(timezone.utc)
    days = delta.days
    if days < 0:
        return "already passed"
    if days == 0:
        return "today"
    return f"in {days} day{'s' if days != 1 else ''}"


def hours_until(iso_ts: str) -> float | None:
    """Returns hours remaining until iso_ts (negative if already passed), or
    None if iso_ts is missing/unparseable."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600


def hours_minutes_left(hours_left: float) -> str:
    if hours_left < 0:
        return "Expired"
    total_minutes = round(hours_left * 60)
    h, m = divmod(total_minutes, 60)
    if h == 0:
        return f"{m}m left"
    return f"{h}h {m}m left"


def format_expiry_est(iso_ts: str) -> str | None:
    """Explicit, always-EST date+time string (e.g. 'Aug 27, 2026, 2:00 PM
    EST') so everyone has one fixed, consistent reference point regardless
    of their own Discord timezone setting or the time of year. Deliberately
    uses a fixed UTC-5 offset year-round rather than America/New_York (which
    would auto-shift to EDT in summer) -- one unchanging label is the whole
    point here, not technically-correct-but-inconsistent labeling."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    from datetime import timedelta
    fixed_est = dt.astimezone(timezone(timedelta(hours=-5)))
    return fixed_est.strftime("%b %d, %Y, %I:%M %p EST").replace(" 0", " ")


def expiring_evolution_embed(item: dict, hours_left: float) -> dict:
    """Compact reminder embed for an evolution approaching its submission
    deadline -- exact time left, explicit EST expiry timestamp, and just
    enough detail (price, unlock requirements) to act on without opening
    fut.gg."""
    evo = item["evolution"]
    base = item.get("basePlayer") or {}
    upgraded = item.get("upgradedPlayer") or {}

    price_bits = []
    if evo.get("coinsCost"):
        price_bits.append(f"{evo['coinsCost']:,} coins")
    if evo.get("pointsCost"):
        price_bits.append(f"{evo['pointsCost']:,} points")
    if evo.get("tokenCost"):
        price_bits.append(f"{evo['tokenCost']:,} tokens")
    price_text = " + ".join(price_bits) if price_bits else "Free"

    name_line = ""
    if base and upgraded:
        name_line = (
            f"{player_name(base)}: {base.get('overall', '?')} -> "
            f"{upgraded.get('overall', '?')} OVR"
        )

    fields = [
        {"name": "Time Left", "value": hours_minutes_left(hours_left), "inline": True},
        {"name": "Price", "value": price_text, "inline": True},
    ]
    expires_est = format_expiry_est(evo.get("endSubmissionTime"))
    if expires_est:
        fields.append({"name": "Expires (EST)", "value": expires_est, "inline": True})
    fields.append(
        {
            "name": "How to Unlock",
            "value": format_kv_lines(evo.get("requirementsText") or [], limit=20),
            "inline": False,
        }
    )

    description = f"**{(evo.get('name') or 'Evolution')[:230]}**"
    if name_line:
        description += f"\n{name_line}"

    embed = {
        "title": "\u23F3 Evolution Expiring Soon",
        "description": description,
        "color": EMBED_COLOR_EXPIRING,
        "fields": fields,
    }
    if evo.get("url"):
        embed["url"] = f"{FUTGG_BASE}{evo['url']}"
    return embed


def check_expiring_evolutions(
    evolutions: list[dict], notified: dict[str, list]
) -> dict[str, list]:
    """Posts a reminder for each live evolution that has newly entered a
    reminder window (see EXPIRY_REMINDER_STAGES) and hasn't been notified
    for that stage yet. Returns the updated notified map. Only ids present
    in `evolutions` are kept -- ids for evolutions no longer live are
    dropped so the state file doesn't grow forever."""
    updated: dict[str, list] = {}
    posted_count = 0

    for item in evolutions:
        evo = item["evolution"]
        evo_id = str(evo["id"])
        hours_left = hours_until(evo.get("endSubmissionTime"))
        already = list(notified.get(evo_id, []))
        updated[evo_id] = already  # carry forward; may append below

        if hours_left is None or hours_left < 0:
            continue  # no deadline data, or it already expired

        for stage_name, stage_hours in EXPIRY_REMINDER_STAGES:
            if hours_left > stage_hours:
                continue  # not in this window yet
            if stage_name in already:
                continue  # already sent this one
            print(
                f"Posting expiring-evolution reminder ({stage_name}): "
                f"{evo.get('name')} -- {hours_left:.1f}h left"
            )
            ok = post_webhook(
                EXPIRING_EVOLUTIONS_WEBHOOK_URL,
                role_mention(EXPIRING_ROLE_ID).strip(),
                expiring_evolution_embed(item, hours_left),
            )
            if ok:
                already.append(stage_name)
                posted_count += 1
                time.sleep(POST_DELAY_SECONDS)
            else:
                print(f"  will retry '{evo.get('name')}' ({stage_name}) on the next run")

    print(f"Expiring evolutions: posted {posted_count} reminder(s).")
    return updated


_CARD_IMAGE_CACHE: dict[str, bytes | None] = {}


def fetch_card_image(url: str) -> bytes | None:
    """Downloads a card image so its numbers can be repainted. Cached per
    run; any failure returns None and the card is used as-is."""
    if not url or url.startswith("data:"):
        return None
    if url in _CARD_IMAGE_CACHE:
        return _CARD_IMAGE_CACHE[url]
    data = None
    try:
        resp = requests.get(url, timeout=15)
        if resp.ok:
            data = resp.content
    except Exception as e:
        print(f"  ! couldn't fetch card image: {e}")
    _CARD_IMAGE_CACHE[url] = data
    return data


def evolved_card_image(url: str, base: dict, upgraded: dict) -> str | None:
    """Returns a data URI for the card with the evolution's resulting face
    stats painted in, or None to fall back to the original artwork."""
    raw = fetch_card_image(url)
    if not raw:
        return None
    try:
        out = cards.composite_evolved_card(raw, base, upgraded)
    except Exception as e:
        print(f"  ! couldn't repaint card stats: {e}")
        return None
    if not out:
        return None
    return "data:image/png;base64," + base64.b64encode(out).decode("ascii")


# The reference layout groups an evolution's upgrades the way the game
# does: the six face stats first, then the detailed in-game attributes,
# then everything that isn't a stat at all.
FACE_STAT_LABELS = {
    "pace", "shooting", "passing", "dribbling", "defending", "physicality",
    "physical",
}
OTHER_LABELS = {
    "overall rating", "overall", "weak foot", "skill moves", "position",
    "rarity", "playstyle", "playstyle+", "roles", "role",
}


def group_upgrades(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Splits totalUpgradesText into (Face stats, Ingame stats, Others),
    dropping any group that ends up empty. An unrecognised label is treated
    as an in-game stat, which is where the long tail of attributes lives."""
    face, ingame, other = [], [], []
    for it in items:
        label = str(it.get("label", "")).strip().lower()
        if label in FACE_STAT_LABELS:
            face.append(it)
        elif any(label.startswith(o) for o in OTHER_LABELS):
            other.append(it)
        else:
            ingame.append(it)
    return [g for g in (("Face stats", face), ("Ingame stats", ingame),
                        ("Others", other)) if g[1]]


def build_evolution_card(item: dict, render_page) -> tuple[dict, bytes, str]:
    """Builds the rendered PNG card plus a minimal Discord embed for a new
    Evolution.

    Same two-panel shape as the SBC and Objective cards: what it costs and
    asks of you on the left, what you get on the right. No player artwork
    -- fut.gg's card image shows the player BEFORE the evolution, so
    printing it beside the evolved stats invites the reader to trust the
    wrong numbers."""
    evo = item["evolution"]
    base = item.get("basePlayer") or {}
    upgraded = item.get("upgradedPlayer") or {}
    game_label = cards.detect_game_label(
        upgraded.get("cardImageUrl"), base.get("cardImageUrl")
    )

    # Coins and FC Points are ALTERNATIVE ways to pay for an evolution, not
    # a combined price -- joining them with "+" overstated the cost.
    options = []
    if evo.get("coinsCost"):
        options.append(("Coins", f"{evo['coinsCost']:,}"))
    if evo.get("pointsCost"):
        options.append(("FC Points", f"{evo['pointsCost']:,}"))
    if evo.get("tokenCost"):
        options.append(("Tokens", f"{evo['tokenCost']:,}"))

    if options:
        left_rows = cards.alternatives_row("Cost", options)
    else:
        # No price doesn't mean free -- an evo with no coin/point cost may
        # be earned another way. Say how when the payload tells us, and
        # only claim Free when nothing suggests otherwise.
        how = cards.extract_acquisition(evo)
        if how:
            left_rows = cards.headline_row(
                "Cost", "Not purchasable", "", truncate(how, 90)
            )
        else:
            left_rows = cards.headline_row("Cost", "Free")

    unlock = relative_days(evo.get("endTime")).replace("in ", "")
    submit = relative_days(evo.get("endSubmissionTime")).replace("in ", "")
    left_rows += cards.meta_tiles([
        ("Unlock within", "" if unlock.lower() == "unknown" else unlock),
        ("Expires in", "" if submit.lower() == "unknown" else submit),
        ("Repeatable",
         "" if evo.get("isRepeatable") is None
         else ("Yes" if evo.get("isRepeatable") else "No")),
    ])

    # Who is ELIGIBLE to use the evolution.
    reqs = evo.get("requirementsText") or []
    if reqs:
        req_rows = ""
        for r in reqs:
            req_rows += cards.compact_row(
                "", truncate(str(r.get("label", "")), 26), format_kv_value(r)
            )
        left_rows += '<div class="subhead">Requirements</div>' + cards.two_col(req_rows)

    # What you actually play to complete it -- separate from eligibility,
    # and missing from the card entirely before.
    challenges = cards.extract_evo_challenges(evo)
    if challenges:
        left_rows += '<div class="subhead">How to Unlock</div>'
        for i, c in enumerate(challenges, 1):
            c_title, c_detail = task_label(c) if isinstance(c, dict) else (str(c), "")
            if c_title:
                left_rows += cards.compact_row(
                    str(i), truncate(c_title, 30), truncate(c_detail, 24)
                )

    # One plain list, no group headings -- the labels say what each stat is
    # and the headings were spending three lines to add nothing. Face stats
    # still lead, since group_upgrades() returns them first.
    upgrades = ""
    for _name, items in group_upgrades(evo.get("totalUpgradesText") or []):
        for r in items:
            upgrades += cards.compact_row(
                "",
                truncate(str(r.get("label", "")), 22),
                str(r.get("value", "")),
                str(r.get("maxValue") or ""),
            )
    if upgrades:
        left_rows += '<div class="subhead">Upgrades Applied</div>' + cards.two_col(upgrades)

    left_html = cards.panel(
        "Evolution Details",
        f"{len(evo.get('totalUpgradesText') or [])} upgrades",
        left_rows,
    )

    enriched = enrich_player(render_page, upgraded)
    name = player_name(enriched) or player_name(base) or "Evolution"
    base_ovr, up_ovr = base.get("overall"), enriched.get("overall")

    right_rows = ""
    if up_ovr is not None:
        sub = (f"up from {base_ovr}"
               if base_ovr is not None and base_ovr != up_ovr else "")
        right_rows += cards.headline_row("Overall", str(up_ovr), "OVR", sub)
    right_rows += cards.evolved_stat_strip(base, enriched)
    right_rows += cards.position_versatility_row(enriched)

    # Roles and PlayStyles are both shown against the pre-evolution player,
    # so what the evo actually GIVES is marked rather than the card just
    # listing what the finished player happens to have. A PlayStyle has two
    # tiers and the upgraded one is written with a SINGLE "+"; the double
    # "++" belongs to Roles, which are a different thing entirely.
    role_names = cards.extract_roles(enriched)
    if role_names:
        base_roles = cards.extract_roles(base)
        right_rows += '<div class="player-section-label">Roles</div>'
        right_rows += (
            '<div class="roles">'
            f'{cards.role_chips(role_names, base_roles if base_roles else None)}'
            "</div>"
        )
    ps = cards.extract_playstyle_names(enriched)
    if ps:
        base_ps = cards.extract_playstyle_names(base)
        right_rows += '<div class="player-section-label">PlayStyles</div>'
        right_rows += (
            '<div class="roles">'
            f'{cards.playstyle_chips(ps, base_ps if base_ps else None)}'
            "</div>"
        )

    positions = cards.extract_positions(enriched)[0] or ""
    right_html = cards.panel(
        "Evolved Player",
        f"{name} · {positions}" if positions else name,
        f'<div class="panel-stack">{right_rows}</div>',
    )

    title = evo.get("name") or "New Evolution"
    html = cards.frame(
        game_label, "NEW EVO", title,
        truncate((evo.get("description") or "").strip(), 200),
        left_html, right_html,
    )
    png = cards.render_card(render_page, html)
    file_name = f"evolution_{evo.get('id', 'x')}.png"

    embed = {
        "title": title[:256],
        "color": EMBED_COLOR_EVOLUTION,
        "image": {"url": f"attachment://{file_name}"},
    }
    if evo.get("url"):
        embed["url"] = f"{FUTGG_BASE}{evo['url']}"
    return embed, png, file_name


_REWARD_NAME_KEYS = ("name", "title", "label", "itemname", "packname", "description")
_REWARD_COIN_KEYS = ("coins", "coinsamount", "coinamount", "coinvalue")
_REWARD_COUNT_KEYS = ("count", "quantity", "amount", "numberofitems")
MAX_REWARD_NOTE_LEN = 80


def _reward_string(value) -> str:
    """A short human string from a payload value, or "" if it isn't one.

    Guards the same way _resolve_icon_slug() does: a value can be a data
    URI, a serialized blob or a URL, and none of those belong in a header
    label."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > MAX_REWARD_NOTE_LEN:
        return ""
    if any(bad in text for bad in ("://", "data:", "\n", "{", "}", "<")):
        return ""
    return text


def award_label(award: dict) -> str:
    """Describes one award, whatever kind of thing it is.

    SBC and objective rewards are not always players -- they're packs,
    coins, kits, badges, tradeable items. Rather than assume a shape, this
    reads whichever of those the payload happens to describe, so the
    header names the actual reward instead of the word "Reward"."""
    if not isinstance(award, dict):
        return ""

    player = award.get("player") or award.get("playerItem")
    if isinstance(player, dict):
        name = player_name(player)
        if name:
            overall = player.get("overall")
            return f"{name} · {overall} OVR" if overall else name

    count = 0
    for key, value in award.items():
        if str(key).lower() in _REWARD_COUNT_KEYS and isinstance(value, int):
            count = value
            break

    for key, value in award.items():
        if str(key).lower() in _REWARD_COIN_KEYS and isinstance(value, int) and value > 0:
            return f"{value:,} coins"

    # A named item, either directly on the award or on a nested item dict
    # (fut.gg wraps some rewards a level down).
    for source in (award, *(v for v in award.values() if isinstance(v, dict))):
        for key, value in source.items():
            if str(key).lower() in _REWARD_NAME_KEYS:
                text = _reward_string(value)
                if text:
                    return f"{count}x {text}" if count > 1 else text
    return ""


def reward_note(item: dict) -> str:
    """The header label naming an item's reward -- a player and rating, a
    pack, coins. Used by both the SBC and objective cards. Names up to two
    awards; returns "" when the payload describes none, leaving the
    panel's own "Reward" heading to stand alone rather than printing it
    twice."""
    labels = []
    for award in item.get("awards") or []:
        text = award_label(award)
        if text and text not in labels:
            labels.append(text)
        if len(labels) == 2:
            break
    return truncate(" + ".join(labels), 46)


def sbc_image_url(sbc: dict) -> str | None:
    """SBC sets have their own artwork at `imagePath` (a relative path, same
    CDN pattern as player card images), but when the reward is actually a
    specific player item, show that player's real card instead -- fut.gg's
    own `imagePath` is sometimes just a generic promo shield (e.g. for
    tournament-reward SBCs), while the reward's card is always the actual
    player being earned."""
    for award in sbc.get("awards") or []:
        player = award.get("player")
        if not player:
            continue
        card_path = player.get("cardImagePath") or player.get("simpleCardImagePath")
        if card_path:
            return f"https://game-assets.fut.gg/cdn-cgi/image/quality=85,format=auto,width=300/{card_path}"

    # Not a player: the SBC's own tile IS the reward artwork, and it's EA's
    # real themed art -- the 83+ upgrade shield with its rating printed on
    # it, the Pre-Season puzzle piece, the FUTTIES pick shield. Do not
    # substitute a generic pack/pick stand-in for it; that trades a good
    # image for a worse one. This whole function is deliberately left
    # exactly as the deployed bot has it, because the live posts prove it
    # resolves the right picture for every reward type.
    if sbc.get("imageUrl"):
        return sbc["imageUrl"]
    if sbc.get("imagePath"):
        return f"https://game-assets.fut.gg/cdn-cgi/image/quality=85,format=auto,width=400/{sbc['imagePath']}"
    return None


def fetch_sbc_challenges(sbc: dict, page) -> list[dict]:
    """Returns an SBC's individual challenges.

    The old note on build_sbc_card said fut.gg only exposes challengesCount
    and not the challenges themselves. That is true of the SBC list API,
    but the SBC's own page does list them by name with their requirements,
    so this loads that page and reads them the same way objectives are
    handled -- and accepts the result only when the number found matches
    challengesCount, so a wrong list can't be shown as if it were right.

    Any failure returns [] and the card falls back to the count alone."""
    if os.environ.get("SKIP_SBC_CHALLENGES", "").lower() == "true":
        return []
    inline = extract_objective_tasks(sbc, sbc.get("challengesCount"))
    if inline:
        return inline
    url = sbc.get("url")
    if not url:
        return []
    url = url if url.startswith("http") else f"{FUTGG_BASE}{url}"
    if url in _PLAYER_DETAIL_CACHE:
        payload = _PLAYER_DETAIL_CACHE[url]
    else:
        payload = {}
        try:
            goto_and_wait_for_router(
                _detail_page(page),
                url,
                """
                () => {
                    const m = window.__TSR_ROUTER__ && window.__TSR_ROUTER__.state.matches;
                    return !!(m && m.some(x => x.loaderData));
                }
                """,
            )
            payload = _detail_page(page).evaluate(
                """
                () => {
                    const matches = window.__TSR_ROUTER__.state.matches.filter(m => m.loaderData);
                    return Object.assign({}, ...matches.map(m => m.loaderData));
                }
                """
            ) or {}
        except Exception as e:
            print(f"  ! couldn't load SBC challenges from {url}: {e}")
        _PLAYER_DETAIL_CACHE[url] = payload
    found = extract_objective_tasks(payload, sbc.get("challengesCount"))
    if not found and payload:
        print(f"  ! no challenge list matching challengesCount="
              f"{sbc.get('challengesCount')} on {url}")
    return found


def build_sbc_card(sbc: dict, render_page) -> tuple[dict, bytes, str]:
    """Builds the rendered PNG card plus a minimal Discord embed for a new
    SBC. The SBC list payload only carries challengesCount, so the named
    challenge breakdown is read off the SBC's own page by
    fetch_sbc_challenges() and validated against that count; if it can't be
    confirmed the panel falls back to the cost and timing rows alone."""
    # Console and PC prices on one line, fut.gg style. When they match (or
    # only one side is priced) it collapses to a single "N coins" rather
    # than printing the same number twice under two different labels.
    # Console and PC each get their own labelled half of the cost block --
    # they price differently and the reader needs to know which figure is
    # theirs. price_split_row() collapses them back to one when the two
    # amounts are equal, and only one platform is listed when only one is
    # priced (rather than implying the other is free).
    prices = []
    if sbc.get("cost"):
        prices.append(("Console", sbc["cost"]))
    if sbc.get("costPc"):
        prices.append(("PC", sbc["costPc"]))

    image_url = sbc_image_url(sbc)
    game_label = cards.detect_game_label(image_url)

    # The panel header names whatever the reward actually is -- a player
    # and rating, a pack, coins -- rather than the word "Reward", which is
    # already the heading beside it.
    reward_label = ""
    reward_caption = ""
    reward_player = None
    for award in sbc.get("awards") or []:
        player = award.get("player")
        if player:
            reward_player = player
            nm = player_name(player)
            if nm:
                reward_label = f"{nm} · {player.get('overall', '?')} OVR"
            break
    if not reward_player:
        # A non-player reward shares the header with the heading in a
        # narrowed column, so a short name goes up there and a long one
        # goes under the art, where it has the full width to wrap into.
        note = reward_note(sbc)
        if len(note) <= 24:
            reward_label = note
        else:
            reward_caption = note

    description = (sbc.get("description") or "").strip()
    # The description is already the card's subtitle and the challenge
    # count is already in the panel header -- neither is repeated here.
    # Cost leads, because that's what people scan an SBC post for; expiry
    # and repeatability pair off beneath it as small facts.
    left_rows = cards.price_split_row("Estimated Cost", prices)
    expires = relative_days(sbc.get("endTime")).replace("in ", "")
    left_rows += cards.meta_tiles([
        # A tile reading "unknown" is worse than no tile: drop it and let
        # the remaining one take the width.
        ("Expires", "" if expires.lower() == "unknown" else expires),
        ("Repeatable",
         "" if sbc.get("isRepeatable") is None
         else ("Yes" if sbc.get("isRepeatable") else "No")),
    ])

    # The squads you actually have to build -- the most useful thing on an
    # SBC and what was leaving this panel half empty. Listed by name when
    # they can be read off the SBC's own page (see fetch_sbc_challenges),
    # otherwise the panel just shows the cost and timing above.
    steps = []
    for c in fetch_sbc_challenges(sbc, render_page):
        c_title, _ = task_label(c)
        if c_title:
            # Show four, then say how many are left rather than dropping
            # them silently -- a squad can carry six and the reader needs
            # to know the list isn't the whole story.
            all_reqs = [truncate(r, 30) for r in task_requirements(c)]
            reqs = all_reqs[:4]
            if len(all_reqs) > 4:
                reqs.append(f"+{len(all_reqs) - 4} more")
            steps.append((truncate(c_title, 30), reqs))
    if steps:
        left_rows += '<div class="subhead">Challenges</div>'
        left_rows += cards.challenge_ladder(steps)
    left_html = cards.panel(
        "SBC Details", f"{sbc.get('challengesCount', '?')} challenges", left_rows
    )

    # The reward panel follows the reward. A player gets the card-shaped
    # hero frame and the player detail beneath it; a pack, a player pick,
    # a set graphic or anything else keeps its own aspect ratio at the
    # width of the (narrowed) column -- these come in several shapes and
    # forcing any of them into a portrait card frame left them shrunk and
    # floating in an empty panel.
    if reward_player and image_url:
        reward_html = (
            f'<div class="card-photo hero" '
            f'style="background-image:url(\'{image_url}\')"></div>'
        )
    elif image_url:
        reward_html = cards.reward_art(image_url)
    else:
        reward_html = '<div class="pack"><div class="glyph">🎁</div></div>'
    if reward_caption:
        reward_html += f'<div class="card-cap"><div class="t">{reward_caption}</div></div>'
    # Whether the reward can be sold on -- EA prints it on the item and it
    # changes what the reward is worth. Omitted when the payload doesn't
    # say, rather than guessed.
    reward_html += cards.trade_badge(reward_tradeability(sbc))

    title = sbc.get("name") or "New SBC"
    # No caption and no stat row under the card: the panel header already
    # carries the player's name and rating, and the card art prints its own
    # face stats. Positions, roles and PlayStyles are kept, since the card
    # can't show those legibly at this size. Each section returns "" when
    # the payload has nothing for it, so none of them leaves a stray
    # heading behind -- which is also what makes them safe to render for a
    # non-player reward, where they all come back empty.
    enriched = enrich_player(render_page, reward_player)
    right_html = cards.panel(
        "Reward", reward_label,
        f"""<div class="reward-panel-body{'' if reward_player else ' mid'}">
          {reward_html}
          {cards.position_versatility_row(enriched)}
          {cards.role_familiarity_row(enriched)}
          {cards.playstyle_badges(cards.extract_playstyle_names(enriched))}
        </div>""",
        centered=True,
    )

    html = cards.frame(
        game_label, "NEW SBC", title,
        truncate(description, 160) or "Complete this squad building challenge to earn the reward.",
        left_html, right_html,
        # A pack doesn't earn half the card's width; give the challenges
        # the room instead.
        compact_reward=not reward_player,
    )
    png = cards.render_card(render_page, html)
    file_name = f"sbc_{sbc.get('id', 'x')}.png"

    embed = {
        "title": title[:256],
        "color": EMBED_COLOR_SBC,
        "image": {"url": f"attachment://{file_name}"},
    }
    if sbc.get("url"):
        embed["url"] = f"{FUTGG_BASE}{sbc['url']}"
    return embed, png, file_name


_PLAYER_DETAIL_CACHE: dict[str, dict] = {}
_DETAIL_PAGE = None


def _detail_page(render_page):
    """A separate tab for loading player pages, opened lazily in the same
    browser context. Kept apart from the render page on purpose: rendering
    works by set_content() on a blank page, and navigating that same page
    out to fut.gg between cards invites timeouts and origin surprises for
    no benefit."""
    global _DETAIL_PAGE
    if _DETAIL_PAGE is None or _DETAIL_PAGE.is_closed():
        _DETAIL_PAGE = render_page.context.new_page()
    return _DETAIL_PAGE


def player_detail_url(player: dict) -> str | None:
    """Builds the fut.gg player page URL for a reward player, if the payload
    carries enough to address one. Accepts either an explicit url/slug or an
    id we can hit the canonical /players/<id>/ route with (fut.gg redirects
    that to the full slug URL)."""
    if not player:
        return None
    url = player.get("url") or player.get("playerUrl")
    if url:
        return url if url.startswith("http") else f"{FUTGG_BASE}{url}"
    slug = player.get("slug")
    if slug:
        return f"{FUTGG_BASE}/players/{slug}/"
    for key in ("eaId", "resourceId", "definitionId", "id"):
        if player.get(key):
            return f"{FUTGG_BASE}/players/{player[key]}/"
    return None


def fetch_player_details(page, player: dict) -> dict:
    """Loads a reward player's own fut.gg page and returns its router
    loaderData, which carries the full player record (PlayStyles, stats,
    positions) that the SBC/objective/evolution list payloads may only
    summarise.

    Uses the same window.__TSR_ROUTER__ technique fetch_evolutions() already
    relies on, so it inherits the same behaviour on fut.gg's ad-script-heavy
    pages. Results are cached per URL for the run (the same player can be the
    reward for more than one item), and ANY failure returns {} -- a card
    still renders from whatever the list payload had, it just shows less.
    Set SKIP_PLAYER_DETAIL=true to turn these extra page loads off."""
    if os.environ.get("SKIP_PLAYER_DETAIL", "").lower() == "true":
        return {}
    url = player_detail_url(player)
    if not url:
        return {}
    if url in _PLAYER_DETAIL_CACHE:
        return _PLAYER_DETAIL_CACHE[url]
    detail = {}
    try:
        goto_and_wait_for_router(
            _detail_page(page),
            url,
            """
            () => {
                const m = window.__TSR_ROUTER__ && window.__TSR_ROUTER__.state.matches;
                return !!(m && m.some(x => x.loaderData));
            }
            """,
        )
        detail = _detail_page(page).evaluate(
            """
            () => {
                const matches = window.__TSR_ROUTER__.state.matches.filter(m => m.loaderData);
                // Merge every match's loaderData -- which one holds the
                // player record has moved between fut.gg releases.
                return Object.assign({}, ...matches.map(m => m.loaderData));
            }
            """
        ) or {}
    except Exception as e:
        print(f"  ! couldn't load player detail from {url}: {e}")
    _PLAYER_DETAIL_CACHE[url] = detail
    return detail


def enrich_player(page, player: dict) -> dict:
    """Returns the reward player merged with anything extra its own fut.gg
    page provides. The list payload always wins on keys it already has, so
    this can only add detail, never overwrite what we were given."""
    if not player:
        return player or {}
    if cards.extract_playstyle_names(player):
        return player          # list payload already had them; no page load
    try:
        detail = fetch_player_details(page, player)
    except Exception as e:
        # Enrichment is strictly a bonus: never let it stop a post going
        # out. Worst case the card renders from the list payload alone.
        print(f"  ! player detail lookup failed, posting without it: {e}")
        return player
    if not detail:
        return player
    merged = dict(detail)
    merged.update(player)
    return merged


def _candidate_task_lists(node, out: list):
    """Collects every list in a payload that looks like a list of tasks:
    dicts carrying a short name/title/description string."""
    if isinstance(node, dict):
        for value in node.values():
            _candidate_task_lists(value, out)
    elif isinstance(node, list):
        entries = [e for e in node if isinstance(e, dict)]
        if entries and len(entries) == len(node):
            named = [
                e for e in entries
                if any(isinstance(e.get(k), str) and e.get(k).strip()
                       for k in ("name", "title", "description", "text"))
            ]
            if len(named) == len(entries):
                out.append(entries)
        for entry in node:
            _candidate_task_lists(entry, out)


def extract_objective_tasks(payload: dict, expected_count: int | None) -> list[dict]:
    """Pulls an objective's individual tasks out of its page payload.

    fut.gg's field names aren't something this can rely on, so instead of
    guessing a key it gathers every task-shaped list and picks the one whose
    length matches the objective's own tasksCount. That count comes from a
    different source (the list payload we already had), so agreeing with it
    is real corroboration rather than a guess -- and when nothing matches
    it returns [] and the card falls back to showing the count alone."""
    if not payload:
        return []
    candidates: list = []
    _candidate_task_lists(payload, candidates)
    if not candidates:
        return []
    if expected_count:
        exact = [c for c in candidates if len(c) == expected_count]
        if exact:
            # Prefer the most detailed of the matching lists.
            return max(exact, key=lambda c: sum(len(e) for e in c))
        return []
    return max(candidates, key=len)


_DETAIL_KEY_HINTS = (
    "requirement", "description", "text", "condition", "detail", "subtitle",
)


def _detail_text(value) -> str:
    """Flattens whatever a detail field holds into one short line.

    fut.gg is inconsistent about this: an objective task's requirement is a
    plain string, while an evolution's requirementsText is a list of
    {label, value} pairs. Rather than guess which shape a given payload
    uses, accept both -- and anything nested one level deeper -- so a
    change on their side doesn't silently blank the column."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        label = str(value.get("label") or value.get("name") or "").strip()
        val = str(value.get("value") or value.get("text") or "").strip()
        return f"{label} {val}".strip()
    if isinstance(value, list):
        parts = [p for p in (_detail_text(v) for v in value) if p]
        return ", ".join(parts)
    return ""


def task_label(task: dict) -> tuple[str, str]:
    """Returns (title, detail) for one task row.

    The detail side matches on what the key *means* rather than on an exact
    field name, for the same reason extract_playstyle_names() does: these
    rows have to keep working when fut.gg renames a field."""
    title = ""
    for key in ("name", "title"):
        if isinstance(task.get(key), str) and task[key].strip():
            title = task[key].strip()
            break
    detail = ""
    for key, value in task.items():
        if not any(hint in str(key).lower() for hint in _DETAIL_KEY_HINTS):
            continue
        candidate = _detail_text(value)
        if candidate and candidate != title:
            detail = candidate
            break
    if not title:
        title, detail = detail, ""
    return title, detail


def _detail_parts(value) -> list[str]:
    """Like _detail_text(), but keeps each requirement separate.

    A real SBC challenge carries several ("MIN overall 77", "MAX 4
    leagues", "MIN 22 total chem"), and joining them into one string meant
    the tail was cut off by the row's length limit. Kept as a list, each
    one gets its own chip and nothing is silently lost."""
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        text = _detail_text(value)
        return [text] if text else []
    if isinstance(value, list):
        parts = []
        for entry in value:
            parts.extend(_detail_parts(entry))
        return parts
    return []


# fut.gg writes requirements out in full ("Min. Squad Total Chemistry
# Points: 22"); EA's own tiles abbreviate ("MIN 22 total chem"). These
# rewrites follow EA's shorthand so a six-requirement challenge fits on
# the card. Anything that matches no pattern is left exactly as written
# rather than mangled -- worst case it reads like fut.gg.
_REQUIREMENT_SHORTHAND = (
    (re.compile(r"^(Min|Max)\.?\s*Squad Total Chemistry Points:\s*(\d+)$", re.I), r"\1. Chem \2"),
    (re.compile(r"^(Min|Max)\.?\s*Team Rating:\s*(\d+)$", re.I), r"\1. Rating \2"),
    (re.compile(r"^(Min|Max)\.?\s*(Clubs|Leagues|Nations|Players) in Squad:\s*(\d+)$", re.I), r"\1. \3 \2"),
    (re.compile(r"^(Min|Max)\.?\s*(\d+)\s*Players? from the same (\w+)$", re.I), r"\1. \2 per \3"),
    (re.compile(r"^(Min|Max)\.?\s*(\d+)\s*Players? from:\s*(.+)$", re.I), r"\1. \2 \3"),
    (re.compile(r"^(Min|Max)\.?\s*(\d+)\s*Players?:\s*(.+)$", re.I), r"\1. \2 \3"),
)


def shorten_requirement(text: str) -> str:
    """One requirement in EA's shorthand, or unchanged if it fits no
    known pattern."""
    text = " ".join((text or "").split())
    for pattern, replacement in _REQUIREMENT_SHORTHAND:
        if pattern.match(text):
            return pattern.sub(replacement, text)
    return text


def task_requirements(task: dict) -> list[str]:
    """Every requirement on one challenge or task, each on its own.

    Same means-not-name key matching as task_label(); this is the list
    form, for the SBC challenge ladder."""
    title, _ = task_label(task)
    for key, value in task.items():
        if not any(hint in str(key).lower() for hint in _DETAIL_KEY_HINTS):
            continue
        parts = [shorten_requirement(p) for p in _detail_parts(value) if p and p != title]
        if parts:
            return parts
    return []


_XP_KEY_HINTS = ("xp", "experience")


def task_reward(task: dict) -> str:
    """What one objective task pays out -- "500 XP", a named pack, or "".

    Same means-not-name matching used elsewhere. XP is checked first
    because it's the common case and it's a number rather than a name;
    anything else falls back to award_label(), so a task paying a pack is
    described the same way an SBC reward is."""
    if not isinstance(task, dict):
        return ""
    for key, value in task.items():
        k = str(key).lower()
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if any(h == k or k.endswith(h) or k.startswith(h) for h in _XP_KEY_HINTS):
            return f"{value:,} XP"
    for key, value in task.items():
        if "reward" in str(key).lower() or "award" in str(key).lower():
            if isinstance(value, dict):
                text = award_label(value)
                if text:
                    return text
            elif isinstance(value, list):
                for entry in value:
                    text = award_label(entry) if isinstance(entry, dict) else ""
                    if text:
                        return text
            else:
                text = _reward_string(value)
                if text:
                    return text
    return ""


_TRADEABLE_KEY_HINTS = ("tradeable", "tradable")


def reward_tradeability(sbc: dict) -> str:
    """"Tradeable" / "Untradeable" / "" for an SBC's reward.

    EA prints this on the reward itself and it changes what the reward is
    worth, so it belongs on the card. The key can be phrased either way
    round (isTradeable / isUntradeable), so the sense is read off the key
    name rather than assumed, and anything unrecognised returns "" instead
    of guessing."""
    def look(node, depth=0):
        if depth > 3 or not isinstance(node, dict):
            return ""
        for key, value in node.items():
            k = str(key).lower()
            if isinstance(value, bool) and any(h in k for h in _TRADEABLE_KEY_HINTS):
                negated = "untradeable" in k or "untradable" in k or k.startswith("not")
                tradeable = (not value) if negated else value
                return "Tradeable" if tradeable else "Untradeable"
        for value in node.values():
            if isinstance(value, dict):
                found = look(value, depth + 1)
                if found:
                    return found
            elif isinstance(value, list):
                for entry in value:
                    found = look(entry, depth + 1)
                    if found:
                        return found
        return ""

    return look(sbc)


def fetch_objective_tasks(page, obj: dict) -> list[dict]:
    """Returns an objective's individual tasks.

    Checks the objective we already have first: fetch_objectives() returns
    whatever fut.gg puts in loaderData.allObjectives, and if the tasks are
    nested in there (tasksCount has to be counting something) no extra work
    is needed. Only when they aren't does this load the objective's own
    page, using the same window.__TSR_ROUTER__ approach fetch_objectives()
    itself uses.

    Cached per run; any failure returns [] so the card still posts with the
    task count alone. Set SKIP_OBJECTIVE_TASKS=true to skip the page loads
    entirely and rely only on the data already in hand."""
    inline = extract_objective_tasks(obj, obj.get("tasksCount"))
    if inline:
        return inline
    if os.environ.get("SKIP_OBJECTIVE_TASKS", "").lower() == "true":
        return []
    slug = obj.get("slug")
    if not slug:
        return []
    url = f"{FUTGG_BASE}/objectives/{slug}/"
    if url in _PLAYER_DETAIL_CACHE:
        payload = _PLAYER_DETAIL_CACHE[url]
    else:
        payload = {}
        try:
            goto_and_wait_for_router(
                _detail_page(page),
                url,
                """
                () => {
                    const m = window.__TSR_ROUTER__ && window.__TSR_ROUTER__.state.matches;
                    return !!(m && m.some(x => x.loaderData));
                }
                """,
            )
            payload = _detail_page(page).evaluate(
                """
                () => {
                    const matches = window.__TSR_ROUTER__.state.matches.filter(m => m.loaderData);
                    return Object.assign({}, ...matches.map(m => m.loaderData));
                }
                """
            ) or {}
        except Exception as e:
            print(f"  ! couldn't load objective tasks from {url}: {e}")
        _PLAYER_DETAIL_CACHE[url] = payload
    tasks = extract_objective_tasks(payload, obj.get("tasksCount"))
    if not tasks and payload:
        print(f"  ! no task list matching tasksCount={obj.get('tasksCount')} on {url}")
    return tasks


def objective_image_url(obj: dict) -> str | None:
    """Objectives don't have their own artwork -- use the first reward's
    player card image, same idea as the SBC fallback."""
    awards = obj.get("awards") or []
    if not awards:
        return None
    first = awards[0]
    if first.get("imageUrl"):
        return first["imageUrl"]
    player_item = first.get("playerItem")
    if player_item and player_item.get("cardImageUrl"):
        return player_item["cardImageUrl"]
    return None


def build_objective_card(obj: dict, render_page) -> tuple[dict, bytes, str]:
    """Builds the rendered PNG card plus a minimal Discord embed for a new
    Objective.

    The objective's individual tasks are listed when they can be read off
    its own fut.gg page (see fetch_objective_tasks); otherwise the card
    falls back to the category and expiry alone. The reward column is
    narrowed unless the reward is an actual player card -- a pack image
    doesn't earn half the card's width.

    Structured like the SBC card, for the same reasons: the fact that
    drives the decision leads, the small facts pair off beneath it, and
    the list of things to do is a numbered ladder."""
    category = (obj.get("category") or {}).get("name", "Objective")
    awards = obj.get("awards") or []
    first = awards[0] if awards else {}
    player_item = first.get("playerItem") or {}
    reward_card_url = player_item.get("cardImageUrl")
    game_label = cards.detect_game_label(reward_card_url)

    description = (obj.get("description") or "").strip()
    tasks = fetch_objective_tasks(render_page, obj)

    # An objective has no price, so the clock is the thing people act on:
    # it leads, the way cost leads on an SBC.
    # The category rides under the clock rather than sitting in a tile of
    # its own, because the tile beside it ("Tasks: 6") only repeated the
    # count the panel header already carries.
    expires = relative_days(obj.get("endTime")).replace("in ", "")
    left_rows = cards.headline_row(
        "Time Left",
        "No deadline given" if expires.lower() == "unknown" else expires,
        "", category,
    )

    steps = []
    for task in tasks:
        t_title, t_detail = task_label(task)
        if not t_title:
            continue
        steps.append((
            truncate(t_title, 34),
            truncate(t_detail, 64),
            truncate(task_reward(task), 18),
        ))
    if steps:
        left_rows += '<div class="subhead">Tasks</div>'
        left_rows += cards.task_ladder(steps)
    # When the tasks can't be read, the panel stops at the clock. The
    # description is already the card's subtitle, so repeating it here as
    # an "About" row would only pad the panel with something the reader
    # has just read.

    left_html = cards.panel(
        "Objective Details",
        f"{len(steps) or obj.get('tasksCount', '?')} tasks",
        left_rows,
    )

    # Same reward treatment as the SBC card: a player gets the hero card
    # frame, anything else keeps its own aspect ratio in the narrowed
    # column, and a failed image load falls back rather than posting a
    # broken-image icon.
    image_url = objective_image_url(obj)
    if reward_card_url:
        reward_html = (
            f'<div class="card-photo hero" '
            f'style="background-image:url(\'{reward_card_url}\')"></div>'
        )
    elif image_url:
        reward_html = cards.reward_art(image_url)
    else:
        reward_html = '<div class="pack"><div class="glyph">🎁</div></div>'

    # The reward's real name and tradeability, not the hardcoded word
    # "Untradeable" this used to print on every objective whether or not
    # it was true. A long name goes under the art, where it can wrap.
    # A player reward keeps the full-width column, so its name goes in the
    # header however long it is -- the same place the SBC card puts it.
    # Only a non-player reward, whose column is narrowed, has to fall back
    # to a caption when the name won't share that header with the heading.
    reward_label, reward_caption = "", ""
    note = reward_note(obj)
    if note:
        if reward_card_url or len(note) <= 24:
            reward_label = note
        else:
            reward_caption = note
    if reward_caption:
        reward_html += f'<div class="card-cap"><div class="t">{reward_caption}</div></div>'
    reward_html += cards.trade_badge(reward_tradeability(obj))

    title = obj.get("name") or "New Objective"
    # No caption repeating the objective's own title (it's the card's
    # heading already) and no stat row repeating what the card art prints.
    enriched = enrich_player(render_page, player_item)
    right_html = cards.panel(
        "Reward", reward_label,
        f"""<div class="reward-panel-body{'' if reward_card_url else ' mid'}">
          {reward_html}
          {cards.position_versatility_row(enriched)}
          {cards.role_familiarity_row(enriched)}
          {cards.playstyle_badges(cards.extract_playstyle_names(enriched))}
        </div>""",
        centered=True,
    )

    html = cards.frame(
        game_label, "NEW OBJECTIVE", title,
        truncate(description, 160) or f"{category} objective — complete all tasks before it expires.",
        left_html, right_html,
        compact_reward=not reward_card_url,
    )
    png = cards.render_card(render_page, html)
    file_name = f"objective_{obj.get('id', 'x')}.png"

    embed = {
        "title": title[:256],
        "color": EMBED_COLOR_OBJECTIVE,
        "image": {"url": f"attachment://{file_name}"},
    }
    if obj.get("slug"):
        embed["url"] = f"{FUTGG_BASE}/objectives/{obj['slug']}/"
    return embed, png, file_name


# ---------------------------------------------------------------------------
# Discord posting
# ---------------------------------------------------------------------------

def post_webhook(
    webhook_url: str,
    content: str,
    embeds: dict | list[dict],
    max_retries: int = 3,
    file_bytes: bytes | None = None,
    file_name: str | None = None,
) -> bool:
    """Post one message to a Discord webhook. `embeds` can be a single embed
    dict (most categories) or a list of embed dicts. If `file_bytes` is
    given (e.g. a rendered card PNG), it's uploaded alongside the embed as a
    multipart attachment -- the embed should reference it via
    `attachment://{file_name}` as its image url. Returns True on success,
    False on failure (after retries) -- never raises, so one bad item can't
    kill the rest of the run."""
    if not webhook_url:
        print("  (no webhook URL configured, skipping post)")
        return False

    if isinstance(embeds, dict):
        embeds = [embeds]

    payload = {
        "content": content,
        "embeds": embeds[:10],  # Discord allows at most 10 embeds per message
        # Explicitly allow role pings in the content. Webhooks can ping a
        # role via this even if that role's own "Allow anyone to @mention
        # this role" setting is off.
        "allowed_mentions": {"parse": ["roles"]},
    }

    for attempt in range(1, max_retries + 1):
        try:
            if file_bytes and file_name:
                resp = requests.post(
                    webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (file_name, file_bytes, "image/png")},
                    timeout=30,
                )
            else:
                resp = requests.post(webhook_url, json=payload, timeout=30)
        except requests.RequestException as e:
            print(f"  ! network error posting to Discord: {e}")
            return False

        if resp.status_code == 429:
            # Rate limited. Discord tells us how long to wait.
            try:
                retry_after = resp.json().get("retry_after", 2)
            except ValueError:
                retry_after = float(resp.headers.get("Retry-After", 2))
            retry_after = float(retry_after) + 0.5
            print(f"  rate limited, waiting {retry_after:.1f}s (attempt {attempt}/{max_retries})")
            time.sleep(retry_after)
            continue

        if 200 <= resp.status_code < 300:
            return True

        print(f"  ! Discord webhook error {resp.status_code}: {resp.text[:300]}")
        return False

    print("  ! gave up after repeated rate limiting")
    return False


# ---------------------------------------------------------------------------
# Generic per-category pipeline (shared by evolutions / SBCs / objectives)
# ---------------------------------------------------------------------------

def process_category(
    label: str,
    items: list[dict],
    get_id,
    get_name,
    build_fn,
    webhook_url: str,
    announce_text: str,
    seen_ids: set,
) -> set:
    """Diffs `items` against `seen_ids`, posts anything new to `webhook_url`,
    and returns the updated set of seen ids (failed posts are left out so
    they're retried on the next run). `build_fn(item)` returns
    (embed, png_bytes, file_name) -- see build_sbc_card / build_evolution_card
    / build_objective_card -- and the rendered PNG is attached to the post
    alongside the (minimal) embed.

    The role ping goes on the FIRST post of a run only. A refresh that
    finds six new SBCs is one event, and pinging the role six times for it
    is what makes people mute the channel; the cards after the first carry
    no content line at all, so the run reads as one announcement followed
    by its cards.

    BACKFILL_COUNT=N overrides the diff for one run and posts the newest N
    items whether or not they've been posted before. It exists to
    republish existing content after a card redesign, so it deliberately
    does NOT change what counts as seen -- normal runs afterwards behave
    as though the backfill never happened."""
    backfill = backfill_count()
    first_run = not seen_ids
    all_ids = {get_id(item) for item in items}

    if backfill:
        new_items = items[:backfill]
        print(f"BACKFILL: reposting {len(new_items)} of {len(items)} {label} "
              f"in payload order, ignoring seen state.")
    else:
        new_items = [] if first_run else [i for i in items if get_id(i) not in seen_ids]
        if first_run:
            print(f"First run for {label}: seeding {len(items)} item(s) without posting.")

    failed_ids = set()
    posted_count = 0
    announced = False
    for i, item in enumerate(new_items):
        name = get_name(item)
        print(f"Posting new {label[:-1] if label.endswith('s') else label}: {name}")
        try:
            embed, png_bytes, file_name = build_fn(item)
        except Exception as e:
            print(f"  ! failed to render card for '{name}', skipping this run: {e}")
            failed_ids.add(get_id(item))
            continue
        # Not `i == 0`: if the first item fails to render, the ping has to
        # ride along with whichever card actually goes out first.
        content = "" if announced else announce_text
        ok = post_webhook(
            webhook_url, content, embed, file_bytes=png_bytes, file_name=file_name
        )
        if ok:
            posted_count += 1
            announced = True
        else:
            failed_ids.add(get_id(item))
            print(f"  will retry '{name}' on the next run")
        if i < len(new_items) - 1:
            time.sleep(POST_DELAY_SECONDS)

    print(f"{label}: posted {posted_count}/{len(new_items)}.")
    if backfill:
        # A backfill republishes what was already announced; letting it
        # rewrite the seen set would drop anything genuinely new that
        # happened to fall outside the first N.
        return seen_ids
    return (seen_ids | all_ids) - failed_ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _AUTO_BACKFILL
    state = load_state()

    # A card redesign republishes a few recent items so the channels show
    # it straight away. Recorded BEFORE anything is posted, and saved
    # immediately: if this run dies halfway through, the next one must not
    # start the backfill over and double-post what already went out. The
    # cost of that ordering is that a crashed run loses the rest of its
    # backfill, which is the right way round -- a partial backfill is a
    # cosmetic loss, a repeated one spams live channels.
    if state.get("card_design_version", 0) != CARD_DESIGN_VERSION:
        _AUTO_BACKFILL = BACKFILL_ON_UPDATE
        print(f"Card design {state.get('card_design_version', 0)} -> "
              f"{CARD_DESIGN_VERSION}: reposting the newest "
              f"{BACKFILL_ON_UPDATE} of each category with the new design.")
        state["card_design_version"] = CARD_DESIGN_VERSION
        save_state(state)

    # Each category is fetched independently and wrapped in its own
    # try/except -- fut.gg's pages occasionally time out (ad/analytics
    # scripts keep the network "busy" indefinitely) or the site changes
    # shape under us. Previously a failure fetching ANY one category (most
    # often evolutions, since it's fetched first) raised an unhandled
    # exception that killed the whole run before SBCs or objectives were
    # even attempted -- so a single flaky page load meant NOTHING posted
    # that run. Now a failure here just means that one category is skipped
    # for this run (nothing is falsely marked as "removed" -- see
    # process_category) and the others still get checked and posted.
    evolutions: list[dict] = []
    sbcs: list[dict] = []
    objectives: list[dict] = []
    evolutions_fetch_ok = False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        print("Fetching evolutions from fut.gg...")
        try:
            evolutions = fetch_evolutions(page)
            evolutions_fetch_ok = True
            print(f"  found {len(evolutions)} live evolutions")
        except Exception as e:
            print(f"  ! failed to fetch evolutions, skipping this category this run: {e}")

        print("Fetching SBCs from fut.gg...")
        try:
            sbcs = fetch_sbcs(page)
            print(f"  found {len(sbcs)} live SBCs")
        except Exception as e:
            print(f"  ! failed to fetch SBCs, skipping this category this run: {e}")

        print("Fetching objectives from fut.gg...")
        try:
            objectives = fetch_objectives(page)
            print(f"  found {len(objectives)} live objectives")
        except Exception as e:
            print(f"  ! failed to fetch objectives, skipping this category this run: {e}")

        browser.close()

    # Card rendering needs its own browser page -- the one used for
    # scraping above is already closed by this point, and rendering only
    # needs a blank page to load our own HTML into (plus real network
    # access to fetch each card's fut.gg image URLs, which the runner has
    # even though this repo's local dev sandbox might not).
    with sync_playwright() as p:
        render_browser = p.chromium.launch()
        render_page = render_browser.new_page(
            # Short viewport on purpose: every card is captured with
            # full_page=True, which takes the LARGER of the content and the
            # viewport, so a tall viewport pads short cards with dead space.
            viewport={"width": 1080, "height": 300}, device_scale_factor=2
        )

        state["evolutions_seen"] = sorted(
            process_category(
                "evolutions",
                evolutions,
                get_id=lambda item: item["evolution"]["id"],
                get_name=lambda item: item["evolution"]["name"],
                build_fn=lambda item: build_evolution_card(item, render_page),
                webhook_url=EVOLUTIONS_WEBHOOK_URL,
                announce_text=f"{role_mention(EVOLUTIONS_ROLE_ID)}New evolution(s) added! \U0001F6A8",
                seen_ids=set(state["evolutions_seen"]),
            )
        )

        state["sbcs_seen"] = sorted(
            process_category(
                "sbcs",
                sbcs,
                get_id=lambda item: item["id"],
                get_name=lambda item: item["name"],
                build_fn=lambda item: build_sbc_card(item, render_page),
                webhook_url=SBC_WEBHOOK_URL,
                announce_text=f"{role_mention(SBC_ROLE_ID)}New SBC(s) added! \U0001F6A8",
                seen_ids=set(state["sbcs_seen"]),
            )
        )

        state["objectives_seen"] = sorted(
            process_category(
                "objectives",
                objectives,
                get_id=lambda item: item["id"],
                get_name=lambda item: item["name"],
                build_fn=lambda item: build_objective_card(item, render_page),
                webhook_url=OBJECTIVES_WEBHOOK_URL,
                announce_text=f"{role_mention(OBJECTIVES_ROLE_ID)}New objective(s) added! \U0001F6A8",
                seen_ids=set(state["objectives_seen"]),
            )
        )

        render_browser.close()

    # Only check/update expiry reminders if the fetch actually succeeded --
    # otherwise an empty `evolutions` list from a failed fetch would look
    # like every evolution disappeared and wipe their notified-state.
    if evolutions_fetch_ok:
        expiry_notified = state.get("evolutions_expiry_notified", {})
        if os.environ.get("FORCE_EXPIRY_REPOST", "").lower() == "true":
            print("FORCE_EXPIRY_REPOST is set: clearing expiry-reminder history for this run.")
            expiry_notified = {}
        state["evolutions_expiry_notified"] = check_expiring_evolutions(
            evolutions, expiry_notified
        )

    save_state(state)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
