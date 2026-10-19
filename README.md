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
5. **Write the prep sheet.** `guestprep-morgan-housel.md` (plus a `.json`) has
   these sections:
   - **Worn out**: questions asked in 3 or more interviews, with timestamped
     links to each time they were asked
   - **Asked more than once**
   - **Threads nobody pulled on**: the guest's passing remarks that no host
     picked up
   - Questions asked only once, collapsed
   - The list of interviews scanned

## Install

```
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```
guestprep "Jane Doe"                          # search + analyse up to 15 interviews
guestprep "Jane Doe" -n 25 --worn-min 4       # wider net, stricter "worn out"
guestprep "Jane Doe" --list-only              # just show what search finds
guestprep "Jane Doe" --url https://youtu.be/... --url ...   # add known appearances
guestprep "Jane Doe" --no-search --urls-file appearances.txt
guestprep "Jane Doe" --transcripts ./transcripts/   # .txt/.vtt/.srt you already have
guestprep "Jane Doe" --query '"Jane Doe" Lex Fridman'   # extra search terms
guestprep "Jane Doe" --no-llm                 # keyword heuristics only, no API key (rough)
```

`--transcripts` covers podcasts that aren't on YouTube. Many shows publish
transcripts on their websites; save them as `.txt` files. Lines may start with
`[mm:ss]` timestamps.

## "Sign in to confirm you're not a bot"

YouTube blocks caption requests from cloud and datacenter IPs. Run guestprep
from your own machine on a normal home or office connection. If you still get
blocked:

- `--cookies cookies.txt`: export cookies from a browser that's logged in to
  YouTube ([how](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp))
- `--proxy http://user:pass@host:port`: use a residential proxy

## Cost

Each transcript is read once and the result is cached, so re-runs only repeat
the grouping step. A 1-2 hour episode is roughly 20-40k input tokens. A
15-interview run on the default model (`claude-opus-5-5`) costs about
$1-3. Use `--model` to pick a different one.

Requests have server-side refusal fallback turned on (`fallbacks: "default"`).
If the model's safety classifier declines a transcript, the API retries it on
a fallback model instead of skipping the episode.

## Tests

```
pip install -e '.[dev]' && pytest
```
