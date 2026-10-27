"""Command-line interface.

wherewerewe run                       # API + worker in one container
wherewerewe import-folder ./Show --title "The Americans"
wherewerewe index "The Americans"
wherewerewe recap "The Americans" --until S02E04
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from .db import create_all, load_settings, session_scope

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Spoiler-safe 'Previously on...' recaps from your own watch history.",
)


def _logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the web UI and API."""
    import uvicorn

    _logging()
    from .config import base_settings

    s = base_settings()
    create_all()
    uvicorn.run(
        "wherewerewe.api.app:app",
        host=host or s.host,
        port=port or s.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


@app.command()
def worker() -> None:
    """Run the background worker (indexing, recaps, sync schedule)."""
    from .worker.runner import Worker

    _logging()
    w = Worker()
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    w.run()


@app.command()
def run() -> None:
    """Run the API server and the worker as two processes (what the container does)."""
    _logging()
    create_all()
    exe = [sys.executable, "-m", "wherewerewe"]
    procs = [subprocess.Popen([*exe, "serve"]), subprocess.Popen([*exe, "worker"])]

    def stop(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    code = 0
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    finally:
        stop()
        for p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
            code = code or (p.returncode or 0)
    raise typer.Exit(code)


@app.command("init-db")
def init_db() -> None:
    """Create database tables."""
    create_all()
    typer.echo("database ready")


@app.command()
def sync(
    library: bool = True,
    history: bool = True,
    full: Annotated[bool, typer.Option(help="Re-read all Tautulli history")] = False,
) -> None:
    """Scan Plex and pull watch history from Tautulli now."""
    from .services import sync as sync_svc

    _logging()
    create_all()
    with session_scope() as session:
        settings = load_settings(session)
        if library:
            typer.echo(f"library: {sync_svc.sync_library(session, settings)}")
        if history:
            result = sync_svc.sync_history(session, settings, full=full)
            typer.echo(f"history: {result['rows']} rows, {len(result['affected'])} user/show pairs")


@app.command("import-folder")
def import_folder(
    folder: Path, title: Annotated[str, typer.Option(help="Show title")], tmdb_id: int | None = None
) -> None:
    """Register a folder of SxxEyy-named videos or subtitle files as a show (no Plex needed)."""
    from .services.sync import import_folder as do_import

    create_all()
    with session_scope() as session:
        try:
            result = do_import(session, folder, title, tmdb_id)
        except FileNotFoundError as exc:
            _fail(f"not a folder: {exc}")
    typer.echo(f"imported {title!r} as show {result['show_id']} with {result['episodes']} episodes")


@app.command()
def extract(path: Path, out: Path | None = None) -> None:
    """Find and clean the dialogue for one video (or subtitle file) and print it."""
    from .config import base_settings
    from .sources.resolver import NoSourceError, resolve_source

    _logging()
    attempts: list[str] = []
    try:
        result = resolve_source(str(path.resolve()), None, base_settings(), attempts)
    except NoSourceError as exc:
        _fail(str(exc))
    typer.secho(
        f"source: {result.detail} (quality {result.quality})", err=True, fg=typer.colors.GREEN
    )
    if out:
        out.write_text(result.text)
        typer.echo(f"wrote {len(result.text.splitlines())} lines to {out}", err=True)
    else:
        typer.echo(result.text)


@app.command()
def shows() -> None:
    """List shows and how much of each is indexed."""
    from sqlalchemy import select

    from .models import Show
    from .services.indexing import show_stats

    create_all()
    with session_scope() as session:
        for show in session.scalars(select(Show).order_by(Show.title)):
            st = show_stats(session, show.id)
            typer.echo(
                f"{show.id:>4}  {show.title}  ({st['summarized']}/{st['episodes']} "
                f"summarized, {st['rollups']}/{st['seasons']} season rollups)"
            )


@app.command()
def index(
    show: str,
    until: Annotated[str | None, typer.Option(help="Only index up to this episode")] = None,
    force: bool = False,
    verbose: bool = False,
) -> None:
    """Extract dialogue and summarize every episode of a show (runs inline)."""
    from sqlalchemy import select

    from .llm.client import get_llm
    from .llm.rollup import build_rollup, season_episode_summaries
    from .models import SeasonRollup
    from .services.indexing import index_episode
    from .services.library import episode_by_code, find_show, regular_episodes

    _logging(verbose)
    create_all()
    with session_scope() as session:
        settings = load_settings(session)
        try:
            s = find_show(session, show)
            limit = episode_by_code(session, s.id, until).order_key if until else None
        except (LookupError, ValueError) as exc:
            _fail(str(exc))
        llm = get_llm(settings)
        episodes = [
            e
            for e in regular_episodes(session, s.id)
            if e.part_of_episode_id is None and (limit is None or e.order_key <= limit)
        ]
        failures = 0
        for ep in episodes:
            started = time.monotonic()
            try:
                msg = index_episode(session, settings, ep.id, force=force, llm=llm)
                session.commit()
                typer.echo(f"{msg} ({time.monotonic() - started:.0f}s)")
            except Exception as exc:
                session.rollback()
                failures += 1
                typer.secho(f"{ep.code}: FAILED: {exc}", fg=typer.colors.RED)
        for season in sorted({e.season for e in episodes}):
            found, missing = season_episode_summaries(session, s.id, season, llm.model)
            if missing or not found:
                continue
            existing = session.scalar(
                select(SeasonRollup).where(
                    SeasonRollup.show_id == s.id,
                    SeasonRollup.season == season,
                    SeasonRollup.model == llm.model,
                )
            )
            if existing is not None and existing.episode_count == len(found) and not force:
                continue
            build_rollup(session, llm, s.id, season)
            session.commit()
            typer.echo(f"season {season}: rollup written")
        # Rollups were built inline; drop the queued duplicates.
        from .models import Job

        for job in session.scalars(
            select(Job).where(Job.type == "rollup_season", Job.status == "queued")
        ):
            if job.payload.get("show_id") == s.id:
                session.delete(job)
    if failures:
        raise typer.Exit(1)


@app.command()
def recap(
    show: str,
    until: Annotated[str, typer.Option(help="Last episode watched, e.g. S02E04 (inclusive)")],
    length: Annotated[str, typer.Option(help="quick, standard or deep")] = "standard",
    verify: Annotated[bool, typer.Option(help="Run the verification pass")] = True,
    show_context: Annotated[bool, typer.Option(help="Print the assembled source material")] = False,
) -> None:
    """Assemble a recap from cached summaries of everything up to and including --until."""
    from .services import recaps
    from .services.library import episode_by_code, find_show

    _logging()
    create_all()
    with session_scope() as session:
        settings = load_settings(session)
        try:
            s = find_show(session, show)
            ep = episode_by_code(session, s.id, until)
            row = recaps.create_recap(
                session,
                settings,
                show_id=s.id,
                length=length,
                boundary_episode_id=ep.id,
                trigger="cli",
                fresh=True,
                enqueue=False,
            )
        except (LookupError, ValueError) as exc:
            _fail(str(exc))
        recap_id = row.id
        if show_context:
            from .llm.recap import build_context

            ctx = build_context(session, s, ep, length, settings, settings.llm_context_tokens)
            typer.secho(ctx.text + "\n" + "-" * 60, err=True, dim=True)
    typer.secho("writing recap...", err=True, dim=True)
    try:
        result = recaps.run_recap(recap_id, verify=verify)
    except Exception as exc:
        _fail(f"recap failed: {exc}")
    if result.status != "done":
        _fail(result.error or "recap failed")
    typer.echo(result.text)
    v = result.verification_json
    if v and v.get("removed"):
        typer.secho(
            f"\n[verification removed {len(v['removed'])} of {v['checked']} sentences]",
            err=True,
            fg=typer.colors.YELLOW,
        )
        for item in v["removed"]:
            reason = f"  ({item['reason']})" if item.get("reason") else ""
            typer.secho(f"  - {item['sentence']}{reason}", err=True, dim=True)
    cov = result.coverage_json or {}
    if cov.get("missing"):
        typer.secho(
            f"[not summarized yet: {', '.join(cov['missing'])}]", err=True, fg=typer.colors.YELLOW
        )


@app.command()
def whois(
    show: str,
    name: str,
    until: Annotated[str, typer.Option(help="Last episode watched, e.g. S02E04")],
) -> None:
    """Answer "who is X again?" using only episodes up to --until."""
    from .llm import prompts
    from .llm.client import get_llm
    from .llm.ledger import ledger_as_of, ledger_notes, normalize_name
    from .services.library import episode_by_code, find_show

    with session_scope() as session:
        settings = load_settings(session)
        try:
            s = find_show(session, show)
            boundary = episode_by_code(session, s.id, until).order_key
        except (LookupError, ValueError) as exc:
            _fail(str(exc))
        needle = normalize_name(name)
        entries = [
            e
            for e in ledger_as_of(session, s.id, boundary)
            if needle in normalize_name(e.name)
            or any(needle in normalize_name(a) for a in e.aliases)
        ]
        if not entries:
            _fail(f"no character matching {name!r} has appeared by {until.upper()}")
        entry = entries[0]
        answer = get_llm(settings).chat(
            [
                {"role": "system", "content": prompts.WHOIS_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.whois_user(f"Who is {name} again?", ledger_notes(entry)),
                },
            ],
            max_tokens=400,
        )
        typer.echo(f"{entry.name} (seen {entry.first_seen}-{entry.last_seen})\n{answer.strip()}")


@app.command("eval")
def spoiler_eval(
    fixtures: Annotated[Path | None, typer.Option(help="Fixture directory")] = None,
    length: str = "standard",
) -> None:
    """Run the spoiler eval set against the configured model and print a report."""
    from .evaluation import load_fixtures, run_fixture

    _logging()
    with session_scope() as session:
        settings = load_settings(session)
    cases = load_fixtures(fixtures)
    failed = 0
    for case in cases:
        report = run_fixture(case, settings, length=length)
        status = "PASS" if report.passed else "FAIL"
        color = typer.colors.GREEN if report.passed else typer.colors.RED
        typer.secho(
            f"{status}  {case.name} (cutoff {case.cutoff}, model {settings.llm_model})", fg=color
        )
        for leak in report.leaks:
            typer.echo(f"    leaked: {leak}")
        if report.removed_by_verifier:
            typer.echo(f"    verifier removed {report.removed_by_verifier} sentence(s)")
        failed += not report.passed
    raise typer.Exit(1 if failed else 0)


if __name__ == "__main__":  # pragma: no cover
    app()
