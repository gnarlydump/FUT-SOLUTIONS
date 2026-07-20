"""
fut.gg -> Discord notifier

Checks fut.gg for newly-added Evolutions, SBCs, and Objectives, and posts
each to its own Discord webhook. Designed to run on a schedule (see
.github/workflows/check.yml) via GitHub Actions, but works fine run locally
too.

How it gets data:
  fut.gg is a client-rendered app (TanStack Start) that embeds its page data
  in a global `window.__TSR_ROUTER__` object once loaded. There's no public
  JSON API, so this script uses Playwright (headless Chromium) to load each
  page for real and pull the data out of that object directly -- the exact
  same data structure the site itself renders from.

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

Trending / best evolved players:
  fut.gg's /evolutions/best/ page lists the highest meta-rated PLAYER cards
  you can currently build through Evolutions (e.g. "Doumbia -- 97.1 meta
  rating"), ranked best-first. For each of the top players in that list, we
  look up their best evolution path (fut.gg's paths API, which returns every
  possible chain for a base card ranked best-first) to get the exact meta
  rating, the evolution chain used to reach it, and card art. Anything newly
  in that top ranking gets posted to a separate "trending" webhook, best
  meta rating first.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

FUTGG_BASE = "https://www.fut.gg"
EVOLUTIONS_URL = f"{FUTGG_BASE}/evolutions/"
SBC_URL = f"{FUTGG_BASE}/sbc/"
OBJECTIVES_URL = f"{FUTGG_BASE}/objectives/"
BEST_EVOLUTIONS_URL = f"{FUTGG_BASE}/evolutions/best/"
# Every possible evolution-chain ("path") for a given base card, ranked
# best-meta-rating-first by fut.gg. {base_ea_id} is the root/un-evolved
# card's EA id.
PATHS_API = f"{FUTGG_BASE}/api/fut/evolutions/v2/26/paths/v2/{{base_ea_id}}/"
# How many of the top-ranked "best evolution players" to track/post per run.
TRENDING_TOP_N = 20
# Never post a trending player below this GG (meta) rating, even if they'd
# otherwise fall within the top N.
MIN_TRENDING_GG_RATING = 93.0

STATE_PATH = Path(__file__).parent / "state" / "state.json"

EVOLUTIONS_WEBHOOK_URL = os.environ.get("EVOLUTIONS_WEBHOOK_URL", "")
SBC_WEBHOOK_URL = os.environ.get("SBC_WEBHOOK_URL", "")
OBJECTIVES_WEBHOOK_URL = os.environ.get("OBJECTIVES_WEBHOOK_URL", "")
TRENDING_EVOLUTIONS_WEBHOOK_URL = os.environ.get("TRENDING_EVOLUTIONS_WEBHOOK_URL", "")

# Optional: Discord role IDs to @-mention when posting. If left blank, the
# post still goes out, just without a role ping. These correspond to the
# "New Evolution" / "New SBC" / "New Objective" reaction roles.
EVOLUTIONS_ROLE_ID = os.environ.get("EVOLUTIONS_ROLE_ID", "")
SBC_ROLE_ID = os.environ.get("SBC_ROLE_ID", "")
OBJECTIVES_ROLE_ID = os.environ.get("OBJECTIVES_ROLE_ID", "")
TRENDING_EVOLUTIONS_ROLE_ID = os.environ.get("TRENDING_EVOLUTIONS_ROLE_ID", "")

EMBED_COLOR_EVOLUTION = 0x5865F2  # discord blurple
EMBED_COLOR_SBC = 0x57F287  # green
EMBED_COLOR_OBJECTIVE = 0xFEE75C  # yellow
EMBED_COLOR_TRENDING = 0xFF6600  # orange

# Discord webhooks are rate-limited (~5 requests per 2 seconds). Posting a
# batch of new items back-to-back with no pause can trip that limit and
# Discord will reject the message. This is the pause between each post.
POST_DELAY_SECONDS = 1.5

DEFAULT_STATE = {
    "evolutions_seen": [],
    "sbcs_seen": [],
    "objectives_seen": [],
    # Base player ids currently in fut.gg's "best evolution players" top
    # ranking as of the last run. This is a snapshot, not an ever-growing
    # set -- a player that rotates out of the ranking and later comes back
    # in will get posted again, since it's a genuinely new appearance on
    # the leaderboard.
    "trending_evolutions_seen": [],
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

def fetch_evolutions(page) -> list[dict]:
    page.goto(EVOLUTIONS_URL, wait_until="networkidle")
    data = page.evaluate(
        """
        () => {
            const m = window.__TSR_ROUTER__.state.matches.find(
                m => m.id === '/evolutions/'
            );
            return m ? m.loaderData.evolutions.data : [];
        }
        """
    )
    return data or []


def fetch_sbcs(page) -> list[dict]:
    page.goto(SBC_URL, wait_until="networkidle")
    data = page.evaluate(
        """
        () => {
            const m = window.__TSR_ROUTER__.state.matches.find(
                m => m.id === '/sbc/_sbcListLayout'
            );
            return m ? m.loaderData.allSbcs : [];
        }
        """
    )
    return data or []


def fetch_objectives(page) -> list[dict]:
    page.goto(OBJECTIVES_URL, wait_until="networkidle")
    data = page.evaluate(
        """
        () => {
            const m = window.__TSR_ROUTER__.state.matches.find(
                m => m.id === '/objectives/_list/_withObjectives'
            );
            return m ? m.loaderData.allObjectives : [];
        }
        """
    )
    return data or []


def path_card_image_url(step: dict) -> str | None:
    p = step.get("futggCardImagePath") or step.get("cardImagePath")
    if not p:
        return None
    return f"https://game-assets.fut.gg/cdn-cgi/image/quality=85,format=auto,width=300/{p}"


def fetch_best_evolution_players(page, limit: int = TRENDING_TOP_N) -> list[dict]:
    """fut.gg's /evolutions/best/ page ranks the highest meta-rated PLAYER
    cards buildable through Evolutions right now, best first. The page only
    gives us name + a link to that player's evolutions page in its embedded
    data, so for each of the top `limit` players we pull their EA id out of
    that link and look up their single best evolution path (fut.gg ranks a
    player's paths best-meta-rating-first) to get the exact rating, the
    evolution chain used, and card art."""
    page.goto(BEST_EVOLUTIONS_URL, wait_until="networkidle")
    player_items = page.evaluate(
        """
        () => {
            const m = window.__TSR_ROUTER__.state.matches.find(
                m => m.id === '/evolutions/best'
            );
            return m ? m.loaderData.playerItems : [];
        }
        """
    ) or []

    results = []
    for item in player_items[:limit]:
        match = re.match(r"/players/(\d+)-", item.get("url") or "")
        if not match:
            continue
        base_ea_id = int(match.group(1))

        paths_result = page.evaluate(
            """
            async (url) => {
                const r = await fetch(url);
                if (!r.ok) return null;
                return r.json();
            }
            """,
            PATHS_API.format(base_ea_id=base_ea_id),
        )
        if not paths_result or not paths_result.get("data"):
            continue

        best_path = paths_result["data"][0]
        final_step = best_path["path"][-1]
        gg_rating = final_step.get("ggRating")
        if gg_rating is not None and gg_rating < MIN_TRENDING_GG_RATING:
            continue
        results.append(
            {
                "id": base_ea_id,
                "name": item.get("name") or player_name(final_step),
                "ggRating": gg_rating,
                "overall": final_step.get("overall"),
                "evoNames": [e["name"] for e in (best_path.get("evolutions") or [])],
                "cardImageUrl": path_card_image_url(final_step),
                "url": f"{FUTGG_BASE}{item['url']}" if item.get("url") else None,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def player_name(p: dict) -> str:
    if p.get("nickname"):
        return p["nickname"]
    return f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()


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
    price_text = " + ".join(price_bits) if price_bits else "Free"

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
            {
                "name": "Requirements",
                "value": format_kv_lines(evo.get("requirementsText") or []),
                "inline": False,
            },
            {
                "name": "Upgrades",
                "value": format_kv_lines(evo.get("totalUpgradesText") or []),
                "inline": False,
            },
        ],
    }
    if evo.get("url"):
        embed["url"] = f"{FUTGG_BASE}{evo['url']}"
    if upgraded.get("cardImageUrl"):
        embed["image"] = {"url": upgraded["cardImageUrl"]}
    if base.get("cardImageUrl"):
        embed["thumbnail"] = {"url": base["cardImageUrl"]}
    return embed


def trending_player_embed(entry: dict, rank: int) -> dict:
    """`entry` is one item built by fetch_best_evolution_players() -- a real
    evolved player card, its meta rating, and the evolution chain used to
    reach it."""
    gg = entry.get("ggRating")
    gg_text = f"{gg:.1f}" if isinstance(gg, (int, float)) else "Unknown"
    chain = " → ".join(entry.get("evoNames") or [])

    embed = {
        "title": f"{entry.get('name', 'Unknown')} — {entry.get('overall', '?')} OVR"[:256],
        "description": f"Ranked **#{rank}** highest meta-rated evolution player right now.",
        "color": EMBED_COLOR_TRENDING,
        "fields": [
            {"name": "Meta Rating", "value": gg_text, "inline": True},
            {"name": "Final Rating", "value": str(entry.get("overall", "?")), "inline": True},
            {"name": "Evolutions Used", "value": str(len(entry.get("evoNames") or [])), "inline": True},
            {"name": "Evolution Path", "value": chain or "Unknown", "inline": False},
        ],
    }
    if entry.get("url"):
        embed["url"] = entry["url"]
    if entry.get("cardImageUrl"):
        embed["image"] = {"url": entry["cardImageUrl"]}
    return embed


def sbc_image_url(sbc: dict) -> str | None:
    """SBCs sometimes have their own image, otherwise fall back to the
    first reward's player card image (fut.gg does the same on its own
    SBC listing)."""
    if sbc.get("imageUrl"):
        return sbc["imageUrl"]
    awards = sbc.get("awards") or []
    if awards and awards[0].get("player") and awards[0]["player"].get("cardImageUrl"):
        return awards[0]["player"]["cardImageUrl"]
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

def post_webhook(webhook_url: str, content: str, embed: dict, max_retries: int = 3) -> bool:
    """Post one message to a Discord webhook. Returns True on success, False
    on failure (after retries) -- never raises, so one bad item can't kill
    the rest of the run."""
    if not webhook_url:
        print("  (no webhook URL configured, skipping post)")
        return False

    payload = {
        "content": content,
        "embeds": [embed],
        # Explicitly allow role pings in the content. Webhooks can ping a
        # role via this even if that role's own "Allow anyone to @mention
        # this role" setting is off.
        "allowed_mentions": {"parse": ["roles"]},
    }

    for attempt in range(1, max_retries + 1):
        try:
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
    they're retried on the next run)."""
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
        ok = post_webhook(webhook_url, announce_text, embed_fn(item))
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

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        print("Fetching evolutions from fut.gg...")
        evolutions = fetch_evolutions(page)
        print(f"  found {len(evolutions)} live evolutions")

        print("Fetching SBCs from fut.gg...")
        sbcs = fetch_sbcs(page)
        print(f"  found {len(sbcs)} live SBCs")

        print("Fetching objectives from fut.gg...")
        objectives = fetch_objectives(page)
        print(f"  found {len(objectives)} live objectives")

        print("Fetching best/trending evolved players from fut.gg...")
        best_players = fetch_best_evolution_players(page)
        print(f"  found {len(best_players)} ranked entries")

        browser.close()

    # Trending / best evolved players leaderboard -- post whatever just
    # entered the top ranking since last run, best meta rating first,
    # picture + evolution path chain included.
    trending_seen_before = set(state.get("trending_evolutions_seen", []))
    if not trending_seen_before:
        print(f"First run for trending evolved players: seeding top {len(best_players)} without posting.")
    else:
        new_entries = [
            (rank, entry) for rank, entry in enumerate(best_players, start=1) if entry["id"] not in trending_seen_before
        ]
        if new_entries:
            print(f"Posting {len(new_entries)} newly-ranked trending player(s)...")
        for i, (rank, entry) in enumerate(new_entries):
            print(f"Posting trending player: {entry.get('name')} (rank {rank}, {entry.get('ggRating')} meta rating)")
            post_webhook(
                TRENDING_EVOLUTIONS_WEBHOOK_URL,
                f"{role_mention(TRENDING_EVOLUTIONS_ROLE_ID)}\U0001F525 New top evolved player -- {entry.get('ggRating', '?')} meta rating!",
                trending_player_embed(entry, rank),
            )
            if i < len(new_entries) - 1:
                time.sleep(POST_DELAY_SECONDS)
    state["trending_evolutions_seen"] = [e["id"] for e in best_players]

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

    save_state(state)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
