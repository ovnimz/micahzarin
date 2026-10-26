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

This installs guestprep and adds two commands to every new Terminal window:
`guestprep` (from any folder) and `guestprep-update` (download the latest
version). Run it again any time; it's safe. You also need a Claude API key:

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
guestprep "Jane Doe" --fast                   # quickest: every step on Sonnet
guestprep "Jane Doe" --no-guide               # questions only, skip the researched guide
guestprep "Jane Doe" --no-llm                 # keyword heuristics only, no API key (rough)
```

`--web` has Claude search the whole web for the guest's appearances. Pages with
a published transcript or a written Q&A are analysed like YouTube transcripts.
YouTube links it finds are added to the YouTube list. Audio-only episodes are
listed at the end of the prep sheet. Results are cached for a week.

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

## Tests

```
pip install -e '.[dev]' && pytest
```
