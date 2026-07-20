# fut.gg → Discord notifier

Posts a message to Discord whenever fut.gg lists a new **Evolution** or a new
**SBC**, each to its own channel. Runs automatically once an hour via GitHub
Actions — no server or always-on computer required.

**Cost: $0, permanently — as long as the repo is public.** GitHub Actions
gives public repos unlimited free minutes. Private repos only get 2,000 free
minutes/month, and an hourly Playwright-based check can burn through a good
chunk of that, so this could eventually start costing money on a private
repo. Nothing sensitive lives in the code, so there's no real downside to
public — see step 2 below.

## How it works

fut.gg doesn't expose a public API, but the site embeds its full page data
(evolutions, SBCs, player stats, images) in the page itself. This script
uses a headless browser (Playwright) to load the page and read that data
directly, compares it against a list of previously-seen items (`state/state.json`,
committed back to the repo after every run), and posts anything new to
Discord via webhook.

**First run is silent.** The first time it runs it has nothing to compare
against, so it records everything currently live as a baseline without
posting — otherwise you'd get 200+ messages dumped in your channel at once.
Every run after that only posts genuinely new items.

## Setup

### 1. Create two Discord webhooks

One for Evolutions, one for SBCs (skip this and reuse one URL for both if
you'd rather they post to the same channel).

For each channel:
1. Open Discord → right-click the channel → **Edit Channel**.
2. Go to **Integrations** → **Webhooks** → **New Webhook**.
3. Name it (e.g. "Evolutions" or "SBCs"), optionally set an avatar.
4. Click **Copy Webhook URL**. Keep this handy — you'll paste it into GitHub next.

### 2. Create a GitHub repo with these files

1. Go to [github.com/new](https://github.com/new), create a new repository
   and set it to **Public** (keeps this at $0 forever — see the cost note
   above). Your webhook URLs stay private regardless, since they're stored
   as encrypted Actions secrets, never committed to the code, and never
   printed in logs.
2. Upload all the files in this folder, preserving the folder structure —
   specifically `.github/workflows/check.yml` needs to stay at that exact
   path. Easiest way: on the repo page, use **Add file → Upload files** and
   drag the whole `futgg-discord-bot` folder contents in (GitHub preserves
   subfolders when you drag a folder in via the web uploader). If you're
   comfortable with git, cloning and `git add . && git commit && git push`
   works too.

### 3. Add your webhook URLs as repo secrets

1. In your new repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Add one named `EVOLUTIONS_WEBHOOK_URL` with the Evolutions webhook URL as the value.
4. Add another named `SBC_WEBHOOK_URL` with the SBC webhook URL as the value.

### 4. Enable and test

1. Go to the **Actions** tab in your repo. If prompted, click **"I understand my
   workflows, go ahead and enable them"**.
2. Click into the **"Check fut.gg for new evolutions and SBCs"** workflow.
3. Click **Run workflow** (top right) to trigger it manually — don't wait for
   the hourly schedule. This first run should complete with no Discord
   messages (it's just seeding the baseline — check the run logs, it'll say
   "First run: seeding N evolutions without posting").
4. Run it again manually. If nothing new was added to fut.gg between the two
   runs, you'll still see no messages — that's correct. To confirm the
   Discord side works end-to-end, you can temporarily delete an id or two
   from `state/state.json` in the repo and re-run; those items will get
   re-posted as if new.

After that, it runs automatically every hour on its own.

## Customizing

- **Frequency:** edit the `cron` line in `.github/workflows/check.yml`
  (currently `"0 * * * *"` = every hour). Cron is in UTC.
- **Message format:** edit `evolution_embed()` / `sbc_embed()` in `bot.py`.
- **One channel instead of two:** just use the same webhook URL for both
  `EVOLUTIONS_WEBHOOK_URL` and `SBC_WEBHOOK_URL` secrets.

## Running locally (optional, for testing)

```bash
pip install -r requirements.txt
playwright install --with-deps chromium
export EVOLUTIONS_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export SBC_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python bot.py
```
