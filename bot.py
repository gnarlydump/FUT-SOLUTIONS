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

FUTGG_BASE = os.environ.get("FUTGG_BASE", "https://www.fut.gg")
# Overridable so a fut.gg path change at the FC 27 rollover is a workflow
# variable, not a code edit and a redeploy. The scrapers already match on
# the SHAPE of the page data rather than route ids, so a rename usually
# only moves the URL.
EVOLUTIONS_URL = os.environ.get("EVOLUTIONS_URL") or f"{FUTGG_BASE}/evolutions/"
SBC_URL = os.environ.get("SBC_URL") or f"{FUTGG_BASE}/sbc/"
OBJECTIVES_URL = os.environ.get("OBJECTIVES_URL") or f"{FUTGG_BASE}/objectives/"
# fut.gg's SBC list page used to embed every SBC in the page's initial
# loaderData (loaderData.allSbcs). fut.gg restructured that page at some
# point to only load category summaries up front and fetch the actual SBC
# sets client-side (via React Query) from this paginated endpoint instead.
# {page} is 1-indexed; the response has {data: [...], next, totalPages}.
SBC_API = os.environ.get("SBC_API") or f"{FUTGG_BASE}/api/fut/sbc/"

STATE_PATH = Path(__file__).parent / "state" / "state.json"

EVOLUTIONS_WEBHOOK_URL = os.environ.get("EVOLUTIONS_WEBHOOK_URL", "")
SBC_WEBHOOK_URL = os.environ.get("SBC_WEBHOOK_URL", "")
OBJECTIVES_WEBHOOK_URL = os.environ.get("OBJECTIVES_WEBHOOK_URL", "")
EXPIRING_EVOLUTIONS_WEBHOOK_URL = os.environ.get("EXPIRING_EVOLUTIONS_WEBHOOK_URL", "")
# Where scraper-health warnings go. Falls back to the evolutions webhook,
# so this works without any new configuration.
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")

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

# Bump this after a change worth showing off. The first run on a version
# the state hasn't recorded reposts the newest BACKFILL_ON_UPDATE items of
# each category, so the change lands in the channels by itself instead of
# waiting for enough new content to appear. Every existing deployment
# reads as 0, so the next run after this upload backfills once.
POST_FORMAT_VERSION = 1
BACKFILL_ON_UPDATE = 5

# The most items one category will post in a single run. A new game turns
# the whole fut.gg catalogue over at once -- every FC 27 item is an id
# we've never seen, so without this the first run after launch would dump
# the entire catalogue into the channel. Anything past the cap is marked
# seen and NOT posted, with a note on the announcement saying how many
# were held back, so the channel stays readable and nobody is left
# wondering what they missed.
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN") or 12)

DEFAULT_STATE = {
    "evolutions_seen": [],
    "sbcs_seen": [],
    "objectives_seen": [],
    # Maps evolution id (as a string, since JSON object keys must be
    # strings) -> list of reminder stage names already posted for it, e.g.
    # {"1234": ["48h"]}. Prevents re-posting the same reminder every hour.
    "evolutions_expiry_notified": {},
    # The post format the channels have already seen. When this doesn't
    # match POST_FORMAT_VERSION the next run reposts a few recent items.
    # 0 means "never recorded", which is where every existing deployment
    # starts.
    "post_format_version": 0,
}


# Set by main() when the state's recorded post format is out of date. Env
# BACKFILL_COUNT still wins, so a manual run can ask for a different size.
_AUTO_BACKFILL = 0


def warn_channel(message: str) -> None:
    """Tells a human that the scraper has stopped working.

    Every failure so far has been silent -- the cron trigger 404ing after
    a repo rename, an asset path that made every render throw. The bot
    logged it, GitHub went green, and the channels just went quiet for
    days. A category that suddenly yields nothing is the same shape of
    problem, so it goes somewhere a person will actually see.

    Falls back to the evolutions webhook when no dedicated alert hook is
    configured, and never raises -- a failed warning must not take the
    run down with it."""
    url = ALERT_WEBHOOK_URL or EVOLUTIONS_WEBHOOK_URL
    print(f"  !! {message}")
    if not url:
        return
    try:
        post_webhook(url, "", {
            "title": "\u26a0\ufe0f fut.gg scraper needs a look",
            "description": message[:4000],
            "color": EMBED_COLOR_EXPIRING,
        })
    except Exception as e:
        print(f"  ! (couldn't post the warning either: {e})")


def backfill_count() -> int:
    """How many items per category this run should repost regardless of
    what's already been posted, or 0 for normal behaviour.

    Only a POSITIVE BACKFILL_COUNT overrides. Anything else -- unset,
    empty, "0", or junk -- falls through to the automatic value. That
    distinction matters: a workflow_dispatch (which is how cron-job.org
    triggers this) passes an input's default on every run, and treating
    that as "suppress" would silently eat the one automatic backfill,
    spending its marker without posting anything."""
    raw = (os.environ.get("BACKFILL_COUNT") or "").strip()
    auto = min(_AUTO_BACKFILL, MAX_BACKFILL)
    if not raw:
        return auto
    try:
        n = int(raw)
    except ValueError:
        print(f"  ! BACKFILL_COUNT={raw!r} is not a number -- ignoring it.")
        return auto
    if n <= 0:
        return auto
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


# Discord caps a single embed field's value at 1024 characters. Anything
# longer is rejected outright, so a long list has to be split across
# fields rather than truncated.
EMBED_FIELD_LIMIT = 1024


def format_kv_lines(items: list[dict], limit: int = 12) -> str:
    """requirementsText / totalUpgradesText are lists of {label, value[,
    maxValue]}. Kept for the expiry reminder, which wants a short list."""
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


def truncate_field(text: str) -> str:
    """Keeps a field value inside Discord's 1024-character limit, cutting
    at a separator so the line never ends mid-name."""
    if len(text) <= EMBED_FIELD_LIMIT:
        return text
    cut = text[:EMBED_FIELD_LIMIT - 2].rsplit(" \u00b7 ", 1)[0]
    return cut + " \u2026"


def kv_fields(name: str, items: list[dict]) -> list[dict]:
    """The SAME list, as however many embed fields it takes to show all of
    it -- no "...and 3 more".

    An evolution can apply sixteen upgrades and the old 12-item cap simply
    dropped the rest, which on a defensive evo meant the tackling stats
    were the ones that vanished. Lines are packed up to Discord's
    per-field limit and continued in a second field, so nothing is lost
    however long the list runs."""
    lines = []
    for item in items:
        label = item.get("label", "")
        value = item.get("value", "")
        max_value = item.get("maxValue")
        lines.append(f"**{label}:** {value} {max_value}" if max_value
                     else f"**{label}:** {value}")
    if not lines:
        return [{"name": name, "value": "None", "inline": False}]

    fields, chunk, size = [], [], 0
    for line in lines:
        # +1 for the newline that will join it to the previous line.
        if chunk and size + len(line) + 1 > EMBED_FIELD_LIMIT:
            fields.append(chunk)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        fields.append(chunk)

    return [
        {
            "name": name if i == 0 else f"{name} (cont.)",
            "value": "\n".join(c),
            "inline": False,
        }
        for i, c in enumerate(fields)
    ]


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
    # Coins, FC Points and tokens are ALTERNATIVE ways to pay for an
    # evolution, not a combined bill -- joining them with "+" overstated
    # the cost.
    price_text = " or ".join(price_bits) if price_bits else "Free"

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


# ---------------------------------------------------------------------------
# PlayStyles and roles
#
# fut.gg's field names for these are not documented and have changed
# before, so nothing here matches on a field NAME. Instead we walk the
# whole player payload and keep any string that IS a known PlayStyle or
# role. That means a rename on their side costs nothing, and a field we've
# never seen still works the moment it carries real names.
#
# The trade-off is the opposite failure: if fut.gg simply doesn't carry
# them, these return [] and the embed shows no section at all -- which is
# the right outcome. An empty "PlayStyles" heading would be worse than none.
# ---------------------------------------------------------------------------

# Every PlayStyle in FC 26. A name FC 27 adds is simply not matched until
# it's added here -- it is never guessed at.
PLAYSTYLE_NAMES = {
    "acrobatic", "aerial fortress", "anticipate", "block", "bruiser",
    "chip shot", "cross claimer", "dead ball", "deflector", "enforcer",
    "far reach", "far throw", "finesse shot", "first touch", "footwork",
    "game changer", "incisive pass", "intercept", "inventive", "jockey",
    "long ball pass", "long throw", "low driven shot", "pinged pass",
    "power shot", "precision header", "press proven", "quick step",
    "rapid", "relentless", "rush out", "slide tackle", "technical",
    "tiki taka", "trickster", "whipped pass"
}

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

# Long strings are never names. Guards against a description or a data URI
# happening to contain "block" or "rapid".
MAX_STYLE_NAME_LEN = 40


def _clean(value) -> str:
    """The comparable form of a payload value, or "" if it can't be a name."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > MAX_STYLE_NAME_LEN:
        return ""
    if any(c in text for c in "/:;{}<>\n"):
        return ""
    return text.rstrip("+").strip().lower()


def _walk_names(node, vocab, on_match, plus=0):
    """Depth-first walk keeping track of whether the KEY PATH implies a
    tier -- fut.gg expresses PlayStyle+ as a separate `playStylesPlus`
    list rather than a suffix on the name."""
    if isinstance(node, dict):
        # A dict that names a style/role AND carries its level numerically
        # -- {"name": "Holding", "familiarity": 2} -- is the whole entry.
        # Read the level off the sibling key and stop: recursing would
        # find the name with no level attached and report Holding+ for
        # what is actually Holding++.
        name_val = node.get("name") or node.get("role") or node.get("label")
        slug = _clean(name_val)
        if slug in vocab:
            level = None
            for key, value in node.items():
                if str(key).lower() in ("familiarity", "level", "tier", "rank"):
                    if isinstance(value, int) and not isinstance(value, bool):
                        level = value
                        break
            raw = str(name_val).strip()
            if raw.endswith("++"):
                tier = 2
            elif raw.endswith("+"):
                tier = 1
            elif level is not None:
                tier = level
            else:
                tier = plus
            on_match(slug, tier)
            return
        for key, value in node.items():
            k = str(key).lower()
            depth = 2 if ("plusplus" in k or "++" in k) else (1 if "plus" in k else plus)
            _walk_names(value, vocab, on_match, depth)
    elif isinstance(node, list):
        for entry in node:
            _walk_names(entry, vocab, on_match, plus)
    else:
        slug = _clean(node)
        if slug in vocab:
            raw = str(node).strip()
            tier = 2 if raw.endswith("++") else (1 if raw.endswith("+") else plus)
            on_match(slug, tier)


def extract_playstyles(player: dict) -> list[str]:
    """PlayStyle names, with a "+" on the upgraded tier.

    A PlayStyle has exactly two tiers, base and +. The double "++" belongs
    to roles and is never used here."""
    if not player:
        return []
    found, order = {}, []

    def note(slug, tier):
        if slug not in found:
            order.append(slug)
            found[slug] = 0
        found[slug] = max(found[slug], min(tier, 1))

    _walk_names(player, PLAYSTYLE_NAMES, note)
    return [slug.title() + ("+" if found[slug] else "") for slug in order]


def extract_roles(player: dict) -> list[str]:
    """Role names with their familiarity suffix.

    Role familiarity has two levels, + and ++, and there is no bare role --
    so anything found without an explicit level is reported as "+", never
    suffix-less."""
    if not player:
        return []
    found, order = {}, []

    def note(slug, tier):
        if slug not in found:
            order.append(slug)
            found[slug] = 1
        found[slug] = max(found[slug], min(max(tier, 1), 2))

    _walk_names(player, ROLE_NAMES, note)
    return [slug.title() + "+" * found[slug] for slug in order]


def gained(after: list[str], before: list[str]) -> list[str]:
    """The entries an evolution ADDS or RAISES.

    Compared per item, so it holds for whatever a future evolution happens
    to change. With no "before" data to compare against there is nothing
    to claim, so it returns nothing rather than presenting the finished
    player's whole list as a gain."""
    if not before:
        return []
    was = {b.rstrip("+").lower(): b.count("+") for b in before}
    out = []
    for item in after:
        name, plus = item.rstrip("+").lower(), item.count("+")
        if name not in was or plus > was[name]:
            out.append(item)
    return out


def evolution_embed(item: dict) -> dict:
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
    # Coins, FC Points and tokens are ALTERNATIVE ways to pay for an
    # evolution, not a combined bill -- joining them with "+" overstated
    # the cost.
    price_text = " or ".join(price_bits) if price_bits else "Free"

    name_line = ""
    if base and upgraded:
        name_line = (
            f"{player_name(base)}: {base.get('overall', '?')} -> "
            f"{upgraded.get('overall', '?')} OVR\n\n"
        )

    description = name_line + (evo.get("description") or "")

    embed = {
        "title": (evo.get("name") or "New Evolution")[:256],
        "description": description[:4000],
        "color": EMBED_COLOR_EVOLUTION,
        "fields": [
            {"name": "Price", "value": price_text, "inline": True},
            {
                "name": "Unlock Within",
                "value": relative_days(evo.get("endTime")),
                "inline": True,
            },
            {
                "name": "Expires In",
                "value": relative_days(evo.get("endSubmissionTime")),
                "inline": True,
            },
        ],
    }

    if evo.get("isRepeatable") is not None:
        embed["fields"].append({
            "name": "Repeatable",
            "value": "Yes" if evo.get("isRepeatable") else "No",
            "inline": True,
        })

    # Every requirement and every upgrade, across as many fields as it
    # takes -- see kv_fields().
    embed["fields"] += kv_fields("Requirements", evo.get("requirementsText") or [])
    embed["fields"] += kv_fields("Upgrades", evo.get("totalUpgradesText") or [])

    # What the evolution gives beyond raw stats. Where we can compare
    # against the pre-evolution player we show only what it ADDS or
    # RAISES, since that is what the reader is deciding on; with nothing
    # to compare we list what the finished player has and say so, rather
    # than passing off their existing PlayStyles as a gain. Either way the
    # section is omitted entirely when there is nothing to show.
    for heading, after, before in (
        ("PlayStyles", extract_playstyles(upgraded), extract_playstyles(base)),
        ("Roles", extract_roles(upgraded), extract_roles(base)),
    ):
        if not after:
            continue
        new_ones = gained(after, before)
        if new_ones:
            name, values = f"{heading} Gained", new_ones
        elif before:
            continue          # nothing new -- the evo doesn't touch these
        else:
            name, values = f"{heading} (after evo)", after
        embed["fields"].append({
            "name": name,
            "value": truncate_field(" \u00b7 ".join(values)),
            "inline": False,
        })

    if evo.get("url"):
        embed["url"] = f"{FUTGG_BASE}{evo['url']}"
    # No card art. fut.gg's image is the player BEFORE the evolution, so
    # showing it next to the upgraded stats invites reading the wrong
    # numbers -- and it pushed the details themselves off the screen.
    return embed


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


def sbc_embed(sbc: dict) -> dict:
    cost_bits = []
    if sbc.get("cost"):
        cost_bits.append(f"{sbc['cost']:,} coins")
    if sbc.get("costPc") and sbc.get("costPc") != sbc.get("cost"):
        cost_bits.append(f"{sbc['costPc']:,} coins (PC)")
    cost_text = " / ".join(cost_bits) if cost_bits else "Unknown"

    embed = {
        "title": (sbc.get("name") or "New SBC")[:256],
        "description": (sbc.get("description") or "")[:4000],
        "color": EMBED_COLOR_SBC,
        "fields": [
            {"name": "Estimated Cost", "value": cost_text, "inline": True},
            {
                "name": "Challenges",
                "value": str(sbc.get("challengesCount", "?")),
                "inline": True,
            },
            {
                "name": "Expires",
                "value": relative_days(sbc.get("endTime")),
                "inline": True,
            },
        ],
    }
    if sbc.get("url"):
        embed["url"] = f"{FUTGG_BASE}{sbc['url']}"
    image_url = sbc_image_url(sbc)
    if image_url:
        embed["image"] = {"url": image_url}
    return embed


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


def objective_embed(obj: dict) -> dict:
    category = (obj.get("category") or {}).get("name", "Objective")

    embed = {
        "title": (obj.get("name") or "New Objective")[:256],
        "description": (obj.get("description") or "")[:4000],
        "color": EMBED_COLOR_OBJECTIVE,
        "fields": [
            {"name": "Category", "value": category, "inline": True},
            {
                "name": "Tasks",
                "value": str(obj.get("tasksCount", "?")),
                "inline": True,
            },
            {
                "name": "Expires",
                "value": relative_days(obj.get("endTime")),
                "inline": True,
            },
        ],
    }
    if obj.get("slug"):
        embed["url"] = f"{FUTGG_BASE}/objectives/{obj['slug']}/"
    image_url = objective_image_url(obj)
    if image_url:
        embed["image"] = {"url": image_url}
    return embed


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
    embed_fn,
    webhook_url: str,
    announce_text: str,
    seen_ids: set,
) -> set:
    """Diffs `items` against `seen_ids`, posts anything new to `webhook_url`,
    and returns the updated set of seen ids (failed posts are left out so
    they're retried on the next run).

    The role ping goes on the FIRST post of a run only. A refresh that
    finds five new objectives is one event, and pinging the role five
    times for it is what makes people mute the channel; the posts after
    the first carry no content line at all, so a run reads as one
    announcement followed by its embeds.

    BACKFILL_COUNT / the automatic one-shot (see POST_FORMAT_VERSION)
    override the diff for one run and repost the newest N items whether or
    not they've been posted before. A backfill deliberately does NOT change
    what counts as seen: letting it would drop anything genuinely new that
    happened to fall outside the first N."""
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

    # A game rollover makes every item new at once. Post a readable number
    # and mark the rest seen rather than dumping a whole catalogue into the
    # channel; the announcement says how many were held back so nobody is
    # left wondering.
    held_back = 0
    if not backfill and len(new_items) > MAX_POSTS_PER_RUN:
        held_back = len(new_items) - MAX_POSTS_PER_RUN
        print(f"  ! {len(new_items)} new {label} in one run -- posting "
              f"{MAX_POSTS_PER_RUN} and marking the other {held_back} as seen "
              f"without posting (MAX_POSTS_PER_RUN).")
        new_items = new_items[:MAX_POSTS_PER_RUN]
    if held_back:
        announce_text = f"{announce_text} (+{held_back} more not shown)"

    failed_ids = set()
    posted_count = 0
    announced = False
    for i, item in enumerate(new_items):
        name = get_name(item)
        print(f"Posting new {label[:-1] if label.endswith('s') else label}: {name}")
        # Not `i == 0`: if the first post fails, the ping has to ride along
        # with whichever one actually goes out first.
        content = "" if announced else announce_text
        ok = post_webhook(webhook_url, content, embed_fn(item))
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
        return seen_ids
    return (seen_ids | all_ids) - failed_ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _AUTO_BACKFILL
    state = load_state()

    # Repost a few recent items so a change lands in the channels straight
    # away. Recorded BEFORE anything posts, and saved immediately: if this
    # run dies halfway through, the next one must not start the backfill
    # over and double-post what already went out. A partial backfill is a
    # cosmetic loss; a repeated one spams live channels.
    if state.get("post_format_version", 0) != POST_FORMAT_VERSION:
        _AUTO_BACKFILL = BACKFILL_ON_UPDATE
        print(f"Post format {state.get('post_format_version', 0)} -> "
              f"{POST_FORMAT_VERSION}: reposting the newest "
              f"{BACKFILL_ON_UPDATE} of each category.")
        state["post_format_version"] = POST_FORMAT_VERSION
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
    fetch_errors: list[str] = []

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
            fetch_errors.append(f"evolutions: {e}")

        print("Fetching SBCs from fut.gg...")
        try:
            sbcs = fetch_sbcs(page)
            print(f"  found {len(sbcs)} live SBCs")
        except Exception as e:
            print(f"  ! failed to fetch SBCs, skipping this category this run: {e}")
            fetch_errors.append(f"SBCs: {e}")

        print("Fetching objectives from fut.gg...")
        try:
            objectives = fetch_objectives(page)
            print(f"  found {len(objectives)} live objectives")
        except Exception as e:
            print(f"  ! failed to fetch objectives, skipping this category this run: {e}")
            fetch_errors.append(f"objectives: {e}")

        browser.close()

    # A category that returns nothing while the state says it used to hold
    # hundreds of ids means fut.gg moved something -- exactly the kind of
    # break that would otherwise just look like a quiet week. Say so out
    # loud; the FC 27 rollover is when this is most likely to happen.
    for label, items, seen_key in (("evolutions", evolutions, "evolutions_seen"),
                                   ("SBCs", sbcs, "sbcs_seen"),
                                   ("objectives", objectives, "objectives_seen")):
        if not items and state.get(seen_key):
            fetch_errors.append(
                f"{label}: fut.gg returned 0 items, but {len(state[seen_key])} "
                f"have been seen before -- the page has probably moved or "
                f"changed shape."
            )
    if fetch_errors:
        warn_channel(
            "The bot could not read fut.gg properly this run:\n\n- "
            + "\n- ".join(fetch_errors)
            + "\n\nNothing was posted for those categories. If this repeats, "
              "the page URL or data shape has likely changed."
        )

    state["evolutions_seen"] = sorted(
        process_category(
            "evolutions",
            evolutions,
            get_id=lambda item: item["evolution"]["id"],
            get_name=lambda item: item["evolution"]["name"],
            embed_fn=evolution_embed,
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
            embed_fn=sbc_embed,
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
            embed_fn=objective_embed,
            webhook_url=OBJECTIVES_WEBHOOK_URL,
            announce_text=f"{role_mention(OBJECTIVES_ROLE_ID)}New objective(s) added! \U0001F6A8",
            seen_ids=set(state["objectives_seen"]),
        )
    )

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
