# guestprep

**Guest prep inversion.** Before an interview, find every podcast the guest
has already done and list the questions they've already been asked, so you
can skip what's worn out and go where nobody has.

```
guestprep "Morgan Housel"
```

1. **Find appearances.** Runs several YouTube searches (`"Name" podcast`,
   `"Name" interview`, ...) with yt-dlp. No YouTube API key needed. Drops
   clips under 20 minutes and duplicate uploads, and tries the guest's own
   channel last because those are mostly solo episodes.
2. **Get transcripts.** Uses YouTube captions (youtube-transcript-api, with
   yt-dlp as a fallback) and caches them in `~/.cache/guestprep`. If a video
   doesn't name the guest in its title or description, it's kept only if the
   guest's name shows up in the transcript.
3. **Extract questions.** Claude reads each transcript (auto-captions have no
   speaker labels, so it works out who's asking) and lists every question put
   to the guest, with timestamps. It also notes threads the guest raised that
   the host never followed up on. It drops videos that turn out not to be
   interviews of the guest.
4. **Find repeats.** Claude groups the same question asked across different
   shows, even when the wording differs, and ranks the groups by how many
   interviews asked them.
5. **Research a guide.** Claude searches and reads the guest's own writing and
   talks, then writes their background, a timeline, their main ideas in depth
   with exact quotes, a reading/listening list in the order to take it, and the
   main criticisms. Every quote is checked word-for-word against its source;
   any that can't be found are flagged. (`--no-guide` skips this.)
6. **Write the prep sheet.** `guestprep-morgan-housel.md` (plus a `.json`) has
   the guide first, then these sections:
   - **Worn out**: questions asked in 3 or more interviews, with timestamped
     links to each time they were asked
   - **Asked more than once**
   - **Fresh questions**: questions none of the interviews asked, built on
     the guest's work and on threads nobody followed up
   - **No-context prompts**: stand-alone prompts the audience can follow
     without knowing the guest's work (thought experiments, forced choices,
     "what would change your mind", ...), picked to suit this guest
   - **Threads nobody pulled on**: the guest's passing remarks that no host
     picked up
   - Questions asked only once, collapsed
   - The list of interviews scanned

## Install

```
bash install.sh
```

This installs guestprep and adds three commands to every new Terminal window:
`guestprep` (from any folder), `channelscope` (your own channel's analytics,
[below](#channelscope)) and `guestprep-update` (download the latest version). Run it again any time; it's safe. You also need a Claude API key:

```
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
```

## Usage

```
guestprep "Jane Doe"                          # search + analyse up to 15 interviews
guestprep "Jane Doe" -n 25 --worn-min 4       # wider net, stricter "worn out"
guestprep "Jane Doe" --list-only              # just show what search finds
guestprep "Jane Doe" --url https://youtu.be/... --url ...   # add known appearances
guestprep "Jane Doe" --no-search --urls-file appearances.txt
guestprep "Jane Doe" --transcripts ./transcripts/   # .txt/.vtt/.srt you already have
guestprep "Claire Field" --about "Australian VET and tertiary education policy expert"   # rule out namesakes
guestprep "Jane Doe" --query '"Jane Doe" Lex Fridman'   # extra search terms
guestprep "Jane Doe" --web                    # also search podcast sites, written interviews, talks
guestprep "Jane Doe" --topic "being a person"  # focus the sheet and the fresh questions on one topic
guestprep "Jane Doe" --writing https://jane.substack.com   # read their Substack (or any post URL)
guestprep "Jane Doe" --fast                   # quickest: every step on Sonnet
guestprep "Jane Doe" --no-guide               # questions only, skip the researched guide
guestprep "Jane Doe" --no-llm                 # keyword heuristics only, no API key (rough)
```

`--web` has Claude search the whole web for the guest's appearances. Pages with
a published transcript or a written Q&A are analysed like YouTube transcripts.
YouTube links it finds are added to the YouTube list. Audio-only episodes are
listed at the end of the prep sheet. Results are cached for a week.

`--writing` takes a Substack address (it reads up to `--max-posts` posts: the
newest five plus the most-liked) or individual article URLs, and can be given
several times. The guide treats that writing as its main source, quotes from it
(checked word for word), and the fresh questions draw on specific posts. It
works even when the guest has few or no recorded interviews.

`--transcripts` covers podcasts that aren't on YouTube. Many shows publish
transcripts on their websites; save them as `.txt` files. Lines may start with
`[mm:ss]` timestamps.

## "Too many requests" (HTTP 429)

YouTube limits how many caption downloads you can make in a short time. When it
hits the limit, guestprep waits and retries. If it still can't continue, it
analyses whatever it already downloaded. To get past the limit:

- wait 30-60 minutes and re-run. Downloaded transcripts are cached, so it
  continues where it left off
- `--cookies-from-browser chrome` (or `safari`, `firefox`): uses your YouTube
  login, which gets a much higher limit. On a Mac, Chrome asks for keychain
  access the first time; click "Always Allow"

## "Sign in to confirm you're not a bot"

YouTube blocks caption requests from cloud and datacenter IPs. Run guestprep
from your own machine on a normal home or office connection. If you still get
blocked:

- `--cookies-from-browser chrome`: uses the login from a browser where you're
  signed in to YouTube
- `--cookies cookies.txt`: the same thing via an exported cookies file ([how](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp))
- `--proxy http://user:pass@host:port`: use a residential proxy

## Speed

Transcripts are read by Claude Sonnet (`--reader-model`) eight at a time,
starting as each one downloads. The web search runs alongside the YouTube
search, and the guide is researched while the questions are grouped. Opus
(`--model`) does the grouping, fresh questions and guide. `--fast` moves those
to Sonnet too. YouTube downloads stay one at a time to avoid rate limits.

## Cost

Each transcript is read once and the result is cached, so re-runs only repeat
the grouping step. A 1-2 hour episode is roughly 20-40k input tokens. A
15-interview run on the default model (`claude-opus-5-5`) costs about
$1-3. `--web` adds up to 15 web searches ($10 per 1,000) plus reading web
transcripts, which can be longer than YouTube ones. Use `--model` to pick a different one.

Requests have server-side refusal fallback turned on (`fallbacks: "default"`).
If the model's safety classifier declines a transcript, the API retries it on
a fallback model instead of skipping the episode.

# channelscope

**Your own channel, explained.** Connects to your YouTube analytics and works
out who your audience is, what makes them click, what keeps them watching, and
where and why they leave. It turns that into a strategy for titles,
thumbnails and interviews.

```
channelscope --next "Jane Doe, ex-FBI negotiator, on getting people to talk"
```

For your most recent 50 long-form videos (`-n` to change):

1. **Pulls the numbers.** Views, watch time, average % viewed, subscribers
   gained, likes, shares, traffic sources, the YouTube searches that found
   each video, the videos it was suggested next to, and the full retention
   curve. Also channel-wide age/gender, countries, devices and subscriber vs.
   non-subscriber viewing.
2. **Gets impressions and CTR** (see below), then scores every video against
   the five uploads either side of it, so growth or a slow month doesn't
   skew the comparison.
3. **Looks at every thumbnail.** It measures brightness, contrast, saturation
   and colourfulness, and Claude tags faces, expression, eye contact, the
   exact overlay text, clutter, readability at phone size, and the title's
   style and promise. Thumbnails are saved in a folder next to the report.
4. **Finds the drop-offs.** It flags places where noticeably more people left
   than is normal for that video, and places people rewatched. Claude reads
   the transcript there (plus the opening minute) and says what was
   happening: a sponsor read, a long setup, a tangent, a story that landed.
5. **Reads the comments.** The top 100 per video. It finds what people liked
   and disliked, what they asked for, questions they wished you'd asked, what
   they say about the host, and which timestamps they quote.
6. **Builds the model.** It compares title and thumbnail traits with CTR,
   views, retention and subscribers, then Claude (Opus) writes the report:
   - **Your audience**: who they are, what they come for, why they click,
     stay and leave, the words they use, what else they watch
   - **Next episode** (with `--next`): titles, thumbnail concepts, an opening
     for the first 60 seconds
   - **Titles and thumbnails**: what works and what to avoid on *this*
     channel, each with evidence and a confidence level; best and worst CTR
     thumbnails side by side
   - **Interviewing**: what to do more and less of, openings, topics that
     hold and lose viewers
   - **Biggest drop-offs / most rewatched**: timestamped links and what was
     happening at each
   - **Content opportunities** and **experiments to run** (e.g. YouTube's Test
     & Compare)
   - The data: every video in a table, the audience, and which traits go with
     better numbers

It writes `channelscope-<channel>.html` (open it in your browser) and the same report as `.md`, with a `-thumbnails/` folder, a `.csv`
(one row per video, opens in Excel/Numbers), a `.json` with everything, and
`-data.md`. The last one is the summary Claude worked from: paste it into a
Claude chat to ask your own follow-up questions.

## Setup (one command)

```
bash setup-channelscope.sh
```

Run it from this folder (after `bash install.sh`, it's also `channelscope-setup` from anywhere). It
installs everything and saves your Claude key. It creates a Google Cloud project with the YouTube APIs
turned on and lets you add your Studio CSV for CTR. It ends by writing your first report and opening it
in your browser. It's safe to run again: finished steps are skipped.

Google doesn't let scripts create the OAuth client (the sign-in key), so for about two minutes the
script opens three Google Cloud pages one at a time. It tells you exactly what to click on each, then
picks up the downloaded key from your Downloads folder by itself. `bash setup-channelscope.sh --reset`
starts over.

<details><summary>Doing it by hand instead</summary>

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and create a project.
2. **APIs & Services > Library**: turn on **YouTube Data API v3**, **YouTube Analytics API** and
   **YouTube Reporting API**.
3. **Google Auth Platform > Get started**: name the app, choose External, and add your email.
   Under **Audience**, add yourself as a test user and leave the app in **Testing** (don't click
   Publish app: that triggers a Google review). Google then asks you to sign in again about once a week.
4. **Clients > Create client > Desktop app**. Download the JSON and save it as
   `~/.config/channelscope/client_secret.json`. Google shows the secret only once.

On the first run a browser opens to sign in. Pick your channel; if it's a Brand Account, pick that
Brand Account. On "Google hasn't verified this app", click Continue: it's your own app.

</details>

## Impressions and CTR

YouTube's Analytics API doesn't include impressions or click-through rate.
There are two ways to get them:

- **Studio export (lifetime numbers, works now).** In YouTube Studio:
  Analytics > Advanced mode > Content. Make sure **Impressions** and
  **Impressions click-through rate** are columns, set the date range to
  Lifetime, then Export > Comma-separated values. Pass the `Table data.csv`
  from the download:
  `channelscope --studio-csv ~/Downloads/Content*/Table\ data.csv`
- **Automatic (fills in over time).** The first run starts YouTube's daily
  reach report through the Reporting API. Reports take up to 48 hours to
  appear and only cover days after that, so CTR builds up with each run.

When both exist for a video, the Studio export wins. The setup script saves your export to
`~/.config/channelscope/studio.csv`, and channelscope uses that file automatically. Rerun the setup to
refresh it.

## Usage

```
channelscope                                   # last 50 long-form videos
channelscope -n 100 --since 2025-01-01         # more history
channelscope --studio-csv "Table data.csv"     # add lifetime CTR
channelscope --next "guest + topic"            # titles/thumbnails/opening for your next episode
channelscope --shorts                          # include Shorts (left out by default)
channelscope --fast                            # cheaper: everything on Sonnet
channelscope --no-llm                          # numbers, stats and drop-off transcripts only, no Claude
channelscope --refresh                         # re-download analytics instead of using today's cache
channelscope --account second-channel          # keep a separate sign-in per channel
channelscope --logout
```

Transcripts come from the same caption download guestprep uses, so the
same "too many requests" / "not a bot" fixes apply (`--cookies-from-browser
chrome`). For your own videos there's also `--captions-api`: it downloads
your captions through the official API instead. It asks for one more
permission and uses 250 of the 10,000 daily quota units per video.

## Cost and limits

- **YouTube quota:** a 50-video run uses about 60-100 of your 10,000 daily
  units (more with `--captions-api`). Analytics, comments, thumbnails and
  transcripts are cached in `~/.cache/channelscope`, so a re-run the same day
  is quick.
- **Claude:** thumbnails and episodes are read by Sonnet, then Opus writes the
  model. With 50 one-hour episodes that's about $3-6 on the first run.
  Re-runs reuse earlier readings (only videos with new comments or retention
  changes are read again), so they mostly cost the final step, under $1.
- **Sample size:** with a few dozen videos the trait statistics are leads to
  test, not rules. The report gives each recommendation a confidence level,
  and the experiments section suggests how to check them.

## Tests

```
pip install -e '.[dev]' && pytest
```
