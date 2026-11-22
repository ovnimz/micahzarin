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
version). Run it again any time; it's safe.

The repository is private, so `guestprep-update` downloads with your GitHub
login through GitHub's `gh` tool. Set that up once:

```
brew install gh
gh auth login      # choose GitHub.com, then log in with the browser
``` You also need a Claude API key:

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
guestprep "Jane Doe" --book ~/Books/jane-doe/   # read PDFs of their books (a file or a folder)
guestprep "Jane Doe" --papers                 # academics: their papers from OpenAlex (abstracts + free PDFs)
guestprep "Jane Doe" --dense                  # a dense thinker: the guide and questions think harder
guestprep "Jane Doe" --fast                   # cheapest: every stage on Sonnet 5.5
guestprep "Jane Doe" --effort xhigh           # one effort for every stage (low/medium/high/xhigh/max)
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

`--book` (or `--pdf`) takes a PDF of the guest's book or paper, or a folder
of PDFs, and can be given several times. Each book is read in full: Claude
reads it in sections of about 100 pages and notes each section's
argument, key ideas, memorable examples and anything personal, with exact
quotes and page numbers. The guide's ideas section is then built from those
notes, quotes the books with links that open the PDF at the right page
(`file:///...#page=42`), and the reading list says which chapters to start
with. Every book quote is checked word for word against the PDF. The fresh
questions draw on specific arguments and examples, with page numbers. The
notes are cached, so a book is only read once per model. It works with no interviews at
all.

- Page numbers are the PDF's own page numbers, which may differ from the
  numbers printed in the book.
- Name the files after the books (`empiricism-and-experience.pdf`). The
  title comes from the PDF's metadata, or from the filename if there's none.
- Scanned PDFs have no text to read. Run them through OCR first
  (`brew install ocrmypdf`, then `ocrmypdf scan.pdf book.pdf`).
- Cost: a 300-page book is roughly 150-250k input tokens, about $1-2.50 per
  book on Opus 5.5 (half that with `--fast`), once.

`--papers` is for academics. PhilPeople, PhilPapers and PhilArchive block
scripts (Cloudflare's "Just a moment..." check), so it uses
[OpenAlex](https://openalex.org), a free, open index of the same journals and
books. It finds the guest's author record (several people usually share a
name, so Claude picks the one matching `--about`, and prints the others), lists
their most cited and newest work with venue, year and abstract for the guide,
and reads up to `--max-papers` free PDFs in full, like `--book`. Some free PDFs
(PhilArchive's especially) won't download from a script; it says how many, and
you can save those from your browser and pass them with `--book`. If it picks
the wrong person, rerun with `--openalex-author A...` using an ID it printed.
OpenAlex's free allowance is shared per network; if it runs out, get a free key
at openalex.org and `export OPENALEX_API_KEY=...`.

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

## Which model does what

Each stage gets the model and effort it needs, and the run prints its choices
at the start:

| Stage | Model | Effort | Why |
|---|---|---|---|
| Reading interviews, web search | Sonnet 5.5 | medium | Careful but mechanical, and most of the tokens |
| Books and papers | Opus 5.5 | high | Dense arguments, exact quotes |
| Grouping repeat questions | Opus 5.5 | medium | Matching the same question worded differently |
| Guide and fresh questions | Opus 5.5 | high, or **xhigh** for a dense guest | Understanding the ideas and finding new angles |

A guest counts as dense when you give it their books or papers (`--book`,
`--papers`) or add `--dense`. To change the choices: `--reader-model` sets the
interview and web stages, `--model` the rest, `--effort` one effort for every
stage, and `--fast` puts every stage on Sonnet 5.5 at medium.

Transcripts are read eight at a time, starting as each one downloads. The web
search runs alongside the YouTube search, and the guide is researched while the
questions are grouped. YouTube downloads stay one at a time to avoid rate
limits.

## Cost

Each transcript is read once and the result is cached, so re-runs only repeat
the grouping step. A 1-2 hour episode is roughly 20-40k input tokens. A
15-interview run costs about $2-4 with the default per-stage models (Sonnet 5.5
at $2/$10 per million input/output tokens for the interviews, Opus 5.5 at
$4/$20 for the rest). `--fast` is a little cheaper; `--model`/`--reader-model
claude-opus-5-5` everywhere roughly doubles the interview reading. `--web` adds up to 15 web searches ($10 per 1,000) plus reading web
transcripts, which can be longer than YouTube ones. Use `--model` to pick a different one.

Requests have server-side refusal fallback turned on (`fallbacks: "default"`).
If the model's safety classifier declines a transcript, the API retries it on
a fallback model instead of skipping the episode.

## Tests

```
pip install -e '.[dev]' && pytest
```
