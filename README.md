# Kankor Quiz Bot

Daily automation: pulls quiz questions from Telegram channels, lets you approve
them from your phone, renders branded image cards, and — after a second,
explicit approval on the actual finished post — publishes them to a Facebook
Page (feed post + Story, with the answer revealed 6 hours later).

There are two separate approval steps, both done from your phone in Telegram:
1. **Question review** — approve/reject individual questions as they're scraped.
2. **Post approval** — once a batch is due to go out, you're shown the exact
   images and caption (with hashtags) that would be posted, with Publish/Reject
   buttons. Nothing reaches Facebook until you tap Publish.

Full design notes are in `kankor_quiz_bot_spec.md`. This file is just the
"how do I actually run it" guide.

## One-time setup

1. Install Python 3.11+ if you don't have it, then from this folder:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```
   (The app also tries to download Chromium by itself the first time you run
   `main.py`, so this step is only needed if that automatic download fails.)

2. Open `config.yaml` and fill in:
   - `telegram.api_id` / `api_hash` — from https://my.telegram.org (already filled in).
   - `telegram.review_bot_token` — from @BotFather (already filled in).
   - `telegram.admin_user_ids` — a list of numeric Telegram user IDs allowed
     to review questions. Get your own ID from @userinfobot. Add more IDs to
     let more than one person review — everyone in the list has equal access,
     there are no separate roles.
   - `sources` — one entry per Telegram channel/group to scrape. Already has
     two; add more the same way, each gets scraped one after another.
   - `facebook.page_id` / `page_access_token` — already filled in.
   - `facebook.hashtags` — a constant string appended to every feed post's
     caption, e.g. `"#کانکور #کانکور_افغانستان #Kankor_Afghanistan"`.
   - `schedule` — post times and how many questions per language per day.

   The rest of each feed post's caption (the intro line above the hashtags)
   lives in `assets/strings.json` under `post_caption`, per language — edit
   that file directly, no code changes needed.

   `config.yaml` contains real API keys and tokens — keep it private, don't
   post it anywhere or commit it to a public repo.

## Running it

There are three separate scripts, run from the project root:

- **`python src/ingest.py`** — scrapes new quiz questions from the configured
  Telegram channels into `kankor.db`. First run per channel does a full
  history scan (can take a few minutes); later runs only check for new
  messages. Run this whenever you want fresh questions (e.g. daily, or on a
  schedule).
  - First time ever: it will ask for your phone number and a login code
    right in the console — that's Telegram logging in the scraper account,
    one time only. After that a `kankor_userbot.session` file remembers it.

- **`python src/review_bot.py`** — the Telegram bot you talk to from your
  phone, and the *only* process that ever talks to Facebook. It does two
  things at once:
  - Question review: send it `/next` to see the next question waiting for
    review, with Approve / Reject / Skip buttons. It also pushes new
    questions to you automatically every ~20 seconds if you don't currently
    have one waiting for a decision.
  - Post approval: whenever `main.py` marks a batch as due, this bot renders
    the actual images and (for feed posts) the caption + hashtags that would
    be posted, sends them to every admin, and waits for a Publish or Reject
    tap. Only Publish actually calls the Facebook API.

- **`python src/main.py`** — the daily scheduler. Every 15 minutes it checks
  whether it's time to send today's approved questions (feed post + Story) or
  the 6-hours-later answer story to Telegram for approval. It never posts to
  Facebook itself — `review_bot.py` does that once you approve. Leave this
  window open, or use Windows Task Scheduler to launch it at login (see
  below) — there's no built-in cron, it's just a plain loop.

You can run all three at once (e.g. three terminal windows), or just
`ingest.py` + `review_bot.py` while you're building up approved questions,
and start `main.py` once you're ready to have it start sending daily batches
for your approval. `review_bot.py` must be running for anything to actually
post — `main.py` alone only queues things up.

## Building the .exe

```
build.bat
```

This produces `dist\kankor-bot.exe`, which runs `main.py` (the scheduler).
Copy `config.yaml` and the `assets\` folder next to the exe before running it.
Note: `main.py` itself never renders anything, so this particular exe doesn't
need Chromium — `review_bot.py` is the one that does (see below).

Before considering the build "done," verify on a clean Windows machine with
no Python installed — build `kankor-review.exe` (see below) and check that:
1. The exe launches and runs at all.
2. First run downloads Chromium automatically (into
   `%LOCALAPPDATA%\ms-playwright`, not next to the exe) with the `[SETUP]`
   message, then works.
3. Second run onward starts fast, no download, and works with internet off
   (aside from the actual Telegram/Facebook API calls).

If the automatic Chromium download doesn't work from inside the frozen exe,
run `playwright install chromium` once manually as a fallback.

## Running ingest.py / review_bot.py as .exe too

`build.bat` only builds `main.py` by default. If you want `ingest.py` or
`review_bot.py` as standalone exes too, run PyInstaller against them the
same way, e.g.:
```
pyinstaller --onefile --add-data "assets;assets" --name kankor-ingest src\ingest.py
pyinstaller --onefile --add-data "assets;assets" --name kankor-review src\review_bot.py
```

## Auto-starting at login (optional)

Windows Task Scheduler → Create Task → Trigger: "At log on" → Action: start
`kankor-bot.exe` (or `python src\main.py`). This is just a Windows setting,
nothing to configure in the app itself.

## Things you should double-check before relying on this daily

- **Subject tagging by position** (`assets/subject_positions.json`) is empty
  by default — ingestion still works fine (falls back to keyword matching,
  then a generic "عمومی" tag), but once you've scraped some real data, look
  at which `set_position` numbers correspond to which subject and fill in
  the `ranges` list, e.g. `{"subject": "ریاضی", "from": 1, "to": 20}`.
- **Pashto strings** in `assets/strings.json` are placeholders — have a
  Pashto speaker confirm the wording before it goes out publicly.
- **Promo-phrase list and Iranian-Farsi lexicon flag list**
  (`assets/promo_phrases.json`, `assets/iran_lexicon.json`) are small
  starter lists — add to them as you notice things slipping through,
  no code changes needed.
