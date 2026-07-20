"""
fut.gg -> Discord notifier

Checks fut.gg for newly-added Evolutions and SBCs and posts them to two
separate Discord webhooks. Designed to run on a schedule (see
.github/workflows/check.yml) via GitHub Actions, but works fine run locally
too.

How it gets data:
  fut.gg is a client-rendered app (TanStack Start) that embeds its page data
  in a global `window.__TSR_ROUTER__` object once loaded. There's no public
  JSON API, so this script uses Playwright (headless Chromium) to load the
  page for real and pull the data out of that object directly -- the exact
  same data structure the site itself renders from.

State:
  Previously-seen Evolution/SBC ids are stored in state/state.json. On the
  very first run (no state file yet), the script seeds the file with
  everything currently live WITHOUT posting -- otherwise you'd get 200+
  messages dumped into your channel on the first run. Every run after that
  only posts genuinely new items.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

FUTGG_BASE = "https://www.fut.gg"
EVOLUTIONS_URL = f"{FUTGG_BASE}/evolutions/"
SBC_URL = f"{FUTGG_BASE}/sbc/"

STATE_PATH = Path(__file__).parent / "state" / "state.json"

EVOLUTIONS_WEBHOOK_URL = os.environ.get("EVOLUTIONS_WEBHOOK_URL", "")
SBC_WEBHOOK_URL = os.environ.get("SBC_WEBHOOK_URL", "")

EMBED_COLOR_EVOLUTION = 0x5865F2  # discord blurple
EMBED_COLOR_SBC = 0x57F287  # green

# Discord webhooks are rate-limited (~5 requests per 2 seconds). Posting a
# batch of new items back-to-back with no pause can trip that limit and
# Discord will reject the message. This is the pause between each post.
POST_DELAY_SECONDS = 1.5


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"evolutions_seen": [], "sbcs_seen": []}


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

    payload = {"content": content, "embeds": [embed]}

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
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    first_run_evos = not state["evolutions_seen"]
    first_run_sbcs = not state["sbcs_seen"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        print("Fetching evolutions from fut.gg...")
        evolutions = fetch_evolutions(page)
        print(f"  found {len(evolutions)} live evolutions")

        print("Fetching SBCs from fut.gg...")
        sbcs = fetch_sbcs(page)
        print(f"  found {len(sbcs)} live SBCs")

        browser.close()

    seen_evo_ids = set(state["evolutions_seen"])
    seen_sbc_ids = set(state["sbcs_seen"])

    new_evolutions = [e for e in evolutions if e["evolution"]["id"] not in seen_evo_ids]
    new_sbcs = [s for s in sbcs if s["id"] not in seen_sbc_ids]

    if first_run_evos:
        print(f"First run: seeding {len(evolutions)} evolutions without posting.")
        new_evolutions = []
    if first_run_sbcs:
        print(f"First run: seeding {len(sbcs)} SBCs without posting.")
        new_sbcs = []

    failed_evo_ids = set()
    posted_evo_count = 0
    for i, item in enumerate(new_evolutions):
        evo = item["evolution"]
        print(f"Posting new evolution: {evo['name']}")
        ok = post_webhook(
            EVOLUTIONS_WEBHOOK_URL,
            "New evolution(s) added! \U0001F6A8",
            evolution_embed(item),
        )
        if ok:
            posted_evo_count += 1
        else:
            failed_evo_ids.add(evo["id"])
            print(f"  will retry '{evo['name']}' on the next run")
        if i < len(new_evolutions) - 1:
            time.sleep(POST_DELAY_SECONDS)

    failed_sbc_ids = set()
    posted_sbc_count = 0
    for i, sbc in enumerate(new_sbcs):
        print(f"Posting new SBC: {sbc['name']}")
        ok = post_webhook(
            SBC_WEBHOOK_URL,
            "New SBC(s) added! \U0001F6A8",
            sbc_embed(sbc),
        )
        if ok:
            posted_sbc_count += 1
        else:
            failed_sbc_ids.add(sbc["id"])
            print(f"  will retry '{sbc['name']}' on the next run")
        if i < len(new_sbcs) - 1:
            time.sleep(POST_DELAY_SECONDS)

    # Mark everything currently live as seen EXCEPT items whose post failed --
    # those stay unseen so they get retried automatically on the next run
    # instead of silently vanishing.
    all_evo_ids = {e["evolution"]["id"] for e in evolutions}
    all_sbc_ids = {s["id"] for s in sbcs}
    state["evolutions_seen"] = sorted((seen_evo_ids | all_evo_ids) - failed_evo_ids)
    state["sbcs_seen"] = sorted((seen_sbc_ids | all_sbc_ids) - failed_sbc_ids)
    save_state(state)

    print(
        f"Done. Posted {posted_evo_count}/{len(new_evolutions)} evolution(s), "
        f"{posted_sbc_count}/{len(new_sbcs)} SBC(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
