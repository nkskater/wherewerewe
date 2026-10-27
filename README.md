# Where Were We

**Spoiler-safe "Previously on..." recaps, written from the episodes *you* have actually watched.**

Where Were We reads each Plex user's watch history from Tautulli, pulls the dialogue for every
watched episode from its subtitles (or transcribes it with Whisper), summarizes each episode
once with a local LLM, and assembles a recap that stops exactly where that viewer left off.
Come back to a show after three weeks and the recap is on your phone before the cold open ends.

Everything runs on your own hardware. No dialogue, watch history, or summaries leave your network.

## Features

- **Per-viewer spoiler boundary**: the furthest episode each Plex user has finished (90% by
  default), in the show's order. Specials, rewatches and half-watched episodes never move it.
- **Watching** page (posters or a table) with a one-tap recap in three lengths: quick,
  standard, or deep (season by season). The recap streams in as it's written, and the browser
  can read it aloud.
- **"Who is this again?"**: a searchable cast list as of your position, and a short answer
  built only from what you've seen.
- **Returning-after-a-gap pushes**: a Tautulli playback-start webhook checks how long it's
  been. If it's past your threshold (default 10 days) and the viewer has an ntfy or Discord
  target, a quick recap is written and pushed there.
- **Verification pass**: every recap sentence is checked against the source summaries, and
  unsupported claims are removed before you see them.
- **Spoiler eval suite**: known shows with a cutoff and a list of later events that must
  never appear. It runs in CI against a real model and doubles as a model benchmark.
- **Bring your own model**: Ollama (detected automatically, uses its native API so the
  context window is honoured) or any OpenAI-compatible endpoint.

## How it works

```
 Tautulli ──webhook──▶ ┌──────────────────────────────┐
 Tautulli API ◀──poll── │      API + web UI (FastAPI)   │ ◀── browser / phone
 Plex API ◀─────────── │      SQLite (or Postgres)     │ ──▶ ntfy / Discord
                       └──────────────┬───────────────┘
                                      │ DB-backed job queue
                       ┌──────────────▼───────────────┐
                       │            Worker             │
                       │ 1. sidecar / embedded subs    │
                       │    (ffmpeg) or Whisper        │
                       │ 2. episode summary (LLM, JSON)│
                       │ 3. character ledger update    │
                       │ 4. season rollup              │
                       │ 5. recap: assemble, write,    │
                       │    verify, notify             │
                       └──────────────────────────────┘
```

1. **Watch history.** Tautulli's `get_history` is polled every 15 minutes, and the webhook
   triggers a sync on stop and watched events. Each user gets a boundary per show.
2. **Dialogue.** The dialogue source is chosen in this order: sidecar subtitles next to the
   file (English, non-forced, SDH preferred because it labels speakers), then embedded text
   tracks extracted with ffmpeg, then faster-whisper on the GPU. Text is cleaned: tags,
   lyrics and credits are stripped, repeats are collapsed, and a few plot-relevant SDH cues
   like `[gunshot]` are kept. Each source gets a dialogue-density score, so forced-narrative
   tracks fall through to the next option. The episode synopsis (TMDB or Plex) is added as
   grounding.
3. **Episode summaries.** Each episode is summarized in isolation into structured JSON
   (plot beats, characters, relationships, open and resolved threads, cliffhanger). The result
   is cached per `(model, prompt_version)` and shared across users. A summary only sees its
   own episode, so it can't contain anything later. Transcripts that don't fit the context
   window are summarized in chunks first.
4. **Recap assembly.** Recaps are hierarchical to fit the context window. Earlier seasons use
   their season rollup. The current season uses its episode summaries, with the last few
   episodes in full detail. Open threads and the final moment are carried forward
   explicitly. The prompt never names the show.
5. **Verification.** A deterministic name check runs first, then an LLM fact-check of every
   sentence. Unsupported sentences are removed. If too many are removed, the recap is
   regenerated once with a stricter instruction.

## Spoiler safety

This is the core design problem; the full writeup is in
[docs/spoiler-safety.md](docs/spoiler-safety.md). In short:

| Leak | Defence |
|------|---------|
| Summaries of later episodes | **Structural.** Assembly only ever loads summaries at or before the boundary. A recap request past a viewer's position is refused. The character ledger only reads states (including names and aliases) up to the boundary. |
| The model's own knowledge of the show | A grounding prompt; no show title in the prompt; a **name check** that removes any sentence with a capitalized name that never appears in the source material; an **LLM verification pass**; a **spoiler eval suite** in CI. |

## Quick start (Docker)

```yaml
# docker-compose.yml (excerpt; see the full file in the repo)
services:
  wherewerewe:
    image: ghcr.io/nkskater/wherewerewe:latest
    ports: ["8765:8765"]
    environment:
      - PLEX_URL=https://192.168.1.100:32400
      - VERIFY_SSL=false
      - TAUTULLI_URL=http://192.168.1.100:8181
      - TAUTULLI_API_KEY=...
      - LLM_BASE_URL=http://192.168.1.100:11434/v1
      - LLM_MODEL=qwen2.5:14b
      - MEDIA_PATH_MAP=/data=/data/media
    volumes:
      - /mnt/user/appdata/wherewerewe:/config
      - /mnt/user/media:/data/media:ro
```

Then open `http://<host>:8765`, check **System** for connection status, press **Sync** on
**Watching**, and index the shows you want recaps for from **Library**. Nothing runs on the GPU
until you ask (set `AUTO_INDEX=true` to index shows when someone starts watching them, and overnight). An Unraid Community Apps template is in [`unraid/`](unraid/wherewerewe.xml).

Two tags are published. `latest` includes the Whisper fallback and its CUDA libraries
(about 5 GB). `latest-lite` leaves Whisper out (about 300 MB compressed); with it, episodes
without subtitles are summarized from their synopsis. To build locally:
`docker build -f docker/Dockerfile [--build-arg WHISPER=false] .`

The image runs two processes, the API server and the worker (`wherewerewe run`). The GPU
reservation in the compose file is only needed for Whisper. Without it, episodes with no
subtitles are summarized from their synopsis, or skipped if there isn't one.

### Configuration

Set these as environment variables, or in a `.env` file (see [`.env.example`](.env.example)).
Everything except `CONFIG_DIR` and `DATABASE_URL` can also be changed on the Settings page.
Values saved there override the environment.

| Variable | Default | Notes |
|----------|---------|-------|
| `PLEX_URL`, `PLEX_TOKEN` | | Plex over `https://<ip>` needs `VERIFY_SSL=false` |
| `TAUTULLI_URL`, `TAUTULLI_API_KEY` | | |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | `http://localhost:11434/v1`, `qwen2.5:14b` | Any OpenAI-compatible endpoint |
| `LLM_API` | `auto` | `ollama`, `openai`, or `auto` (probes `/api/version`) |
| `LLM_CONTEXT_TOKENS` | `16384` | Sent to Ollama as `num_ctx`; also sets the assembly budget |
| `VERIFY_MODEL` | same as `LLM_MODEL` | Model for the verification pass |
| `MEDIA_PATH_MAP` | | `<plex path>=<container path>;...` |
| `WHISPER_MODEL`, `WHISPER_DEVICE` | `large-v3`, `cuda` | `WHISPER_ENABLED=false` to turn it off |
| `TMDB_API_KEY` | | Optional synopsis grounding (v3 key or v4 token) |
| `NTFY_URL`, `NTFY_TOKEN`, `DISCORD_WEBHOOK_URL` | | Defaults; each viewer can have their own target |
| `WATCHED_PERCENT` | `90` | When an episode counts as finished |
| `DEFAULT_GAP_DAYS` | `10` | Days away before a recap is pushed (per-viewer override) |
| `PUBLIC_URL` | `http://localhost:8765` | Used for links in notifications |
| `WEBHOOK_SECRET` | | If set, the webhook needs `?token=<secret>` |
| `AUTO_INDEX` | `false` | Index a show when someone starts it, and nightly at `BACKFILL_HOUR` (default 3) |
| `SUBTITLE_LANGUAGES` | `en,eng` | Preferred subtitle languages, in order |
| `CONFIG_DIR`, `DATABASE_URL` | `/config`, SQLite | `postgresql+psycopg://...` for Postgres |

### Tautulli webhook

In Tautulli, go to **Settings → Notification Agents → Add → Webhook**. Set the URL to
`http://<host>:8765/webhooks/tautulli` and the method to POST. Enable the **Playback Start**,
**Playback Stop** and **Watched** triggers, and use this JSON data for all three:

```json
{"action": "{action}", "media_type": "{media_type}", "user_id": "{user_id}",
 "username": "{username}", "rating_key": "{rating_key}",
 "grandparent_rating_key": "{grandparent_rating_key}",
 "season_num": "{season_num}", "episode_num": "{episode_num}"}
```

## Command line

The CLI works without Plex, which makes it useful for trying models and prompts:

```bash
wherewerewe import-folder "/media/TV/The Americans" --title "The Americans"  # SxxEyy files
wherewerewe index "The Americans" --until S02E04
wherewerewe recap "The Americans" --until S02E04 --length standard   # --until is inclusive
wherewerewe whois "The Americans" Gaad --until S02E04
wherewerewe extract "/media/TV/The Americans/S01E01.mkv"  # print the cleaned dialogue
wherewerewe eval                                          # spoiler eval vs the configured model
wherewerewe sync | serve | worker | run
```

## API

| Method | Route | Purpose |
|--------|-------|---------|
| `POST` | `/webhooks/tautulli` | Playback start/stop/watched events |
| `GET` | `/api/users`, `/api/users/{id}/shows` | Viewers; shows in progress with position and days since |
| `PUT` | `/api/users/{id}` | Notification target, gap threshold |
| `POST` | `/api/recaps` | `{show_id, user_id, length, until?, fresh?}` |
| `GET` | `/api/recaps/{id}` | JSON, or SSE with `?stream=true` / `Accept: text/event-stream` |
| `GET` | `/api/shows/{id}/characters?user=` | Character ledger as of the user's position |
| `POST` | `/api/shows/{id}/whois` | "Who is X again?" |
| `POST` | `/api/shows/{id}/index` | Queue extraction and summarization |
| `GET` | `/api/jobs`, `POST /api/jobs/{id}/retry` | Queue status, failures |
| `GET/PUT` | `/api/settings` | Runtime settings (secrets masked) |
| `GET` | `/api/health` | Plex, Tautulli, LLM, ffmpeg, Whisper |

Interactive docs are served at `/docs`.

## Development

```bash
cd backend
uv sync --extra postgres          # add --extra whisper for faster-whisper
uv run pytest                     # unit + integration tests (ffmpeg tests need ffmpeg)
uv run ruff check . && uv run ruff format --check .
EVAL_LLM_BASE_URL=http://192.168.1.100:11434/v1 EVAL_LLM_MODEL=qwen2.5:14b \
  uv run pytest -m eval tests/spoiler_eval -s     # live spoiler eval
pre-commit install
```

The frontend is server-rendered Jinja templates plus one dependency-free script
(`backend/wherewerewe/web/`). There is no build step, and the API and UI ship from the same
process.

Layout: `api/` routes, `worker/` queue and job runner, `sources/` subtitles, ffmpeg, Whisper
and TMDB, `llm/` client, prompts, summaries, rollups, recap assembly, verification and
ledger, `integrations/` Plex, Tautulli and notifiers, `services/` sync, progress, indexing and
recaps, `models/` database. Tests are in `backend/tests/`, and the spoiler fixtures are in
`backend/tests/spoiler_eval/fixtures/`.

## Roadmap

Not built yet: Jellyfin as a second media server, OCR for image-based subtitles (PGS/VobSub),
speaker diarization on the Whisper path, a server-side TTS narrated recap (the browser's
speech synthesis is used for now), and a demo mode with bundled public-domain episodes.

## License

[MIT](LICENSE).
