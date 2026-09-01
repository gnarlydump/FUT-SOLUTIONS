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

import json
import os
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

DEFAULT_STATE = {
    "evolutions_seen": [],
    "sbcs_seen": [],
    "objectives_seen": [],
    # Maps evolution id (as a string, since JSON object keys must be
    # strings) -> list of reminder stage names already posted for it, e.g.
    # {"1234": ["48h"]}. Prevents re-posting the same reminder every hour.
    "evolutions_expiry_notified": {},
}


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


def build_evolution_card(item: dict, render_page) -> tuple[dict, bytes, str]:
    """Builds the rendered PNG card plus a minimal Discord embed (title/url/
    color/image-attachment) for a new Evolution. render_page is an
    already-open Playwright page (see cards.render_card) reused across a
    run's cards rather than opening a new browser per item."""
    evo = item["evolution"]
    base = item.get("basePlayer") or {}
    upgraded = item.get("upgradedPlayer") or {}

    game_label = cards.detect_game_label(
        upgraded.get("cardImageUrl"), base.get("cardImageUrl")
    )

    price_bits = []
    if evo.get("coinsCost"):
        price_bits.append(f"{evo['coinsCost']:,} coins")
    if evo.get("pointsCost"):
        price_bits.append(f"{evo['pointsCost']:,} points")
    if evo.get("tokenCost"):
        price_bits.append(f"{evo['tokenCost']:,} tokens")
    price_text = " + ".join(price_bits) if price_bits else "Free"

    req_items = (evo.get("requirementsText") or [])[:3]
    upg_items = (evo.get("totalUpgradesText") or [])[:3]
    req_rows = "".join(
        cards.row("🔒", r.get("label", ""), format_kv_value(r), True) for r in req_items
    ) or cards.row("✅", "No restrictions", "Open to eligible players")
    upg_rows = "".join(
        cards.row("⬆️", r.get("label", ""), format_kv_value(r)) for r in upg_items
    ) or cards.row("—", "No upgrade data", "")

    left_html = f"""<div class="panel">
      <div class="panel-head"><span>Entry Requirements</span><span class="count">{len(evo.get('requirementsText') or [])} conditions</span></div>
      <div class="panel-body" style="flex:0.5;">{req_rows}</div>
      <div class="subhead">Upgrades Applied</div>
      <div class="panel-body" style="flex:0.5; padding-top:0;">{upg_rows}</div>
    </div>"""

    base_img = base.get("cardImageUrl")
    upg_img = upgraded.get("cardImageUrl")
    base_html = (
        f'<div class="card-photo mini" style="background-image:url(\'{base_img}\')"></div>'
        if base_img else '<div class="card-photo mini"></div>'
    )
    upg_html = (
        f'<div class="card-photo" style="background-image:url(\'{upg_img}\')"></div>'
        if upg_img else '<div class="card-photo"></div>'
    )
    name_line = (
        f"{player_name(base)}: {base.get('overall', '?')} → {upgraded.get('overall', '?')} OVR"
        if base and upgraded else ""
    )
    right_html = cards.panel(
        "Evolution", "Base → Upgraded",
        f"""<div class="reward-panel-body">
          <div class="evo-pair">{base_html}<div class="arrow">→</div>{upg_html}</div>
          <div class="card-cap"><div class="t">{name_line}</div><div class="s">{price_text}</div></div>
        </div>""",
        centered=True,
    )

    title = evo.get("name") or "New Evolution"
    description = (evo.get("description") or "").strip()
    html = cards.frame(
        game_label, "NEW EVOLUTION", title,
        truncate(description, 160) or "Unlock and upgrade an eligible player through staged challenges.",
        left_html, right_html,
        "⚡", f"{title} | EVOLUTION",
        truncate(description, 120) or "New evolution available.",
        f"⏱ Unlock within {relative_days(evo.get('endTime'))}",
        f'<div class="chip status">⚠ Submit by {relative_days(evo.get("endSubmissionTime"))}</div>',
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
    if sbc.get("imageUrl"):
        return sbc["imageUrl"]
    if sbc.get("imagePath"):
        return f"https://game-assets.fut.gg/cdn-cgi/image/quality=85,format=auto,width=400/{sbc['imagePath']}"
    return None


def build_sbc_card(sbc: dict, render_page) -> tuple[dict, bytes, str]:
    """Builds the rendered PNG card plus a minimal Discord embed for a new
    SBC. NOTE: fut.gg's SBC API (see SBC_API / fetch_sbcs()) only gives us
    challengesCount, not each individual challenge's own requirement text
    the way evolutions expose requirementsText -- so unlike the Evolution
    card, this one can't show a real per-challenge breakdown. It shows what
    the API actually gives us (cost, challenge count, expiry, description).
    If fut.gg's SBC payload turns out to carry per-challenge requirements
    under a different key, extending this to a real per-row breakdown is a
    small follow-up, not a redesign."""
    cost_bits = []
    if sbc.get("cost"):
        cost_bits.append(f"{sbc['cost']:,} coins")
    if sbc.get("costPc") and sbc.get("costPc") != sbc.get("cost"):
        cost_bits.append(f"{sbc['costPc']:,} coins (PC)")
    cost_text = " / ".join(cost_bits) if cost_bits else "Unknown"

    image_url = sbc_image_url(sbc)
    game_label = cards.detect_game_label(image_url)

    reward_label = "Reward"
    for award in sbc.get("awards") or []:
        player = award.get("player")
        if player:
            nm = player_name(player)
            if nm:
                reward_label = f"{nm} · {player.get('overall', '?')} OVR"
            break

    description = (sbc.get("description") or "").strip()
    left_rows = cards.row("🪙", "Estimated Cost", cost_text, True)
    left_rows += cards.row("🧩", "Challenges", str(sbc.get("challengesCount", "?")))
    left_rows += cards.row("⏱", "Expires", relative_days(sbc.get("endTime")), True)
    if description:
        left_rows += cards.row("📋", "Details", truncate(description, 180))
    left_html = cards.panel(
        "SBC Details", f"{sbc.get('challengesCount', '?')} challenges", left_rows
    )

    reward_html = (
        f'<div class="card-photo" style="background-image:url(\'{image_url}\')"></div>'
        if image_url else '<div class="pack"><div class="glyph">🎁</div></div>'
    )
    title = sbc.get("name") or "New SBC"
    right_html = cards.panel(
        "Reward", reward_label,
        f"""<div class="reward-panel-body">
          {reward_html}
          <div class="card-cap"><div class="t">{title}</div><div class="s">{reward_label}</div></div>
        </div>""",
        centered=True,
    )

    html = cards.frame(
        game_label, "NEW SBC", title,
        truncate(description, 160) or "Complete this squad building challenge to earn the reward.",
        left_html, right_html,
        "🛡️", f"{title} | SBC",
        truncate(description, 120) or "New squad building challenge available.",
        f"⏱ Expires {relative_days(sbc.get('endTime'))}",
        f'<div class="chip">🧩 {sbc.get("challengesCount", "?")} challenges</div>',
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
    Objective. Same caveat as build_sbc_card(): fut.gg gives us tasksCount,
    not each task's own text, so the task list shows what's actually
    available rather than a fabricated per-task breakdown."""
    category = (obj.get("category") or {}).get("name", "Objective")
    awards = obj.get("awards") or []
    first = awards[0] if awards else {}
    player_item = first.get("playerItem") or {}
    reward_card_url = player_item.get("cardImageUrl")
    game_label = cards.detect_game_label(reward_card_url)

    description = (obj.get("description") or "").strip()
    left_rows = cards.row("🏷️", "Category", category, True)
    left_rows += cards.row("☑️", "Tasks", str(obj.get("tasksCount", "?")))
    left_rows += cards.row("⏱", "Expires", relative_days(obj.get("endTime")), True)
    if description:
        left_rows += cards.row("📋", "Details", truncate(description, 180))
    left_html = cards.panel("Objective Details", category, left_rows)

    image_url = objective_image_url(obj)
    if reward_card_url:
        reward_html = f'<div class="card-photo" style="background-image:url(\'{reward_card_url}\')"></div>'
    elif image_url:
        reward_html = f'<div class="pack" style="background-image:url(\'{image_url}\')"></div>'
    else:
        reward_html = '<div class="pack"><div class="glyph">🎁</div></div>'

    title = obj.get("name") or "New Objective"
    right_html = cards.panel(
        "Reward", "Untradeable",
        f"""<div class="reward-panel-body">
          {reward_html}
          <div class="card-cap"><div class="t">{title}</div><div class="s">{category}</div></div>
        </div>""",
        centered=True,
    )

    html = cards.frame(
        game_label, "NEW OBJECTIVE", title,
        truncate(description, 160) or f"{category} objective — complete all tasks before it expires.",
        left_html, right_html,
        "🎯", f"{title} | OBJECTIVE",
        truncate(description, 120) or "New objective available.",
        f"⏱ Expires {relative_days(obj.get('endTime'))}",
        f'<div class="chip status">☐ TASKS 0/{obj.get("tasksCount", "?")}</div>',
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
    alongside the (minimal) embed."""
    first_run = not seen_ids
    all_ids = {get_id(item) for item in items}
    new_items = [] if first_run else [i for i in items if get_id(i) not in seen_ids]

    if first_run:
        print(f"First run for {label}: seeding {len(items)} item(s) without posting.")

    failed_ids = set()
    posted_count = 0
    for i, item in enumerate(new_items):
        name = get_name(item)
        print(f"Posting new {label[:-1] if label.endswith('s') else label}: {name}")
        try:
            embed, png_bytes, file_name = build_fn(item)
        except Exception as e:
            print(f"  ! failed to render card for '{name}', skipping this run: {e}")
            failed_ids.add(get_id(item))
            continue
        ok = post_webhook(
            webhook_url, announce_text, embed, file_bytes=png_bytes, file_name=file_name
        )
        if ok:
            posted_count += 1
        else:
            failed_ids.add(get_id(item))
            print(f"  will retry '{name}' on the next run")
        if i < len(new_items) - 1:
            time.sleep(POST_DELAY_SECONDS)

    print(f"{label}: posted {posted_count}/{len(new_items)}.")
    return (seen_ids | all_ids) - failed_ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    state = load_state()

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
            viewport={"width": 1080, "height": 900}, device_scale_factor=2
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
