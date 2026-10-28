"""Up Next: what each viewer should watch next, and why.

Everything comes from how people actually watch (Tautulli history), what's in the library
(Plex), and what TMDB knows about each show. No model is needed; the model only writes an
optional pitch when someone asks for one.

1. How you watch. Every show you've touched gets a state (watching, paused, finished,
   sampled = tried an episode or two and stopped) and an affinity from -1 to 1: finishing
   or bingeing a show says you liked it, dropping it after one episode says you didn't.
   Your own Love / Not for me / Seen it marks override that.
2. Your taste. Each show is a weighted set of features (genres, TMDB keywords, creators,
   networks, lead cast, rating, episode length, decade), weighted by how rare they are in
   the library. Your taste is the sum of your shows' features, weighted by affinity and
   how recently you watched them.
3. Scoring a show you haven't started:
   - taste: how well it matches your taste
   - related: TMDB lists it as related to shows you liked (or the other way round)
   - quality: TMDB's rating, discounted when few people voted
   - household: other viewers here finished or are hooked on it
   - fresh: just added to Plex
   The list is then diversified so it isn't ten versions of the same show.
4. Every pick carries its reasons, in plain words, from the parts that scored it.

Spoiler safety: only show-level text (the premise) is used anywhere here.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Episode, Preference, Show, ShowMeta, TmdbTitle, User, WatchEvent, WatchProgress
from .meta import plex_machine_id

PREF_KINDS = ("love", "nope", "seen", "later")
WATCHING_DAYS = 45  # watched within this many days: still "watching"
SAMPLED_DAYS = 21  # one or two episodes and nothing for this long: "sampled"
LANE_SIZE = 24
TOGETHER_MAX_GAP = 12  # episodes apart, at most, to suggest watching a show together

# Feature weights by kind. Rare features also count for more (idf).
KIND_WEIGHT = {
    "g": 1.0,  # genre
    "k": 0.7,  # TMDB keyword
    "c": 1.4,  # creator
    "n": 0.5,  # network / studio
    "a": 0.5,  # lead cast
    "r": 0.3,  # content rating band
    "t": 0.4,  # episode length
    "d": 0.3,  # decade
    "o": 0.2,  # country
    "l": 0.6,  # original language
}
# TMDB keywords that say nothing about whether you'd like a show
NOISE_KEYWORDS = {
    "woman director",
    "duringcreditsstinger",
    "aftercreditsstinger",
    "no opening credits",
    "love",
    "tv series",
}
SCORE_WEIGHTS = {"taste": 0.38, "related": 0.34, "quality": 0.12, "household": 0.10, "fresh": 0.06}

_GENRE_ALIASES = {
    "science fiction": "sci-fi",
    "science-fiction": "sci-fi",
    "sci fi": "sci-fi",
    "suspense": "thriller",
    "talk": "talk show",
    "mini-series": "miniseries",
    "children": "kids",
    "politics": "war & politics",
    "war": "war & politics",
}
_GENRE_LABELS = {"sci-fi": "Sci-Fi", "war & politics": "War & Politics", "talk show": "Talk Show"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def norm_genres(genres: list[str]) -> list[str]:
    """'Sci-Fi & Fantasy' -> ['sci-fi', 'fantasy']; Plex and TMDB names land on the same set."""
    out: list[str] = []
    for g in genres:
        g = g.lower().strip()
        if g in ("war & politics",):
            parts = [g]
        else:
            parts = [p.strip() for p in re.split(r"\s*(?:&|/|,| and )\s*", g) if p.strip()]
        for p in parts:
            p = _GENRE_ALIASES.get(p, p)
            if p not in out:
                out.append(p)
    return out


def genre_label(g: str) -> str:
    return _GENRE_LABELS.get(g, g.title())


def rating_band(rating: str | None) -> str | None:
    if not rating:
        return None
    r = rating.upper()
    if r in ("TV-MA", "R", "NC-17", "18", "MA15+"):
        return "mature"
    if r in ("TV-14", "PG-13", "14", "15", "M"):
        return "teen"
    return "family"


# ---- how a viewer watched each show ------------------------------------------------------


@dataclass
class Watch:
    """One viewer's history with one show."""

    show_id: int
    eps: int  # distinct regular episodes finished
    plays: int  # finished plays, rewatches included
    first: datetime
    last: datetime
    days: int  # distinct days they watched it on
    recent: int = 0  # episodes finished in the last 14 days
    state: str = ""
    affinity: float = 0.0
    weight: float = 1.0  # how much this show says about their taste today

    @property
    def pace(self) -> float:
        return self.eps / max(1, self.days)


@dataclass
class Library:
    """What's in Plex, loaded once per request."""

    shows: dict[int, Show]
    meta: dict[int, ShowMeta]
    counts: dict[int, int]  # regular episodes (one per file)
    hours: dict[int, float]
    seasons: dict[int, int]
    by_tmdb: dict[int, int]
    vectors: dict[int, dict[str, float]] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    last: dict[int, set[int]] = field(default_factory=dict)  # each show's final episode id(s)


def load_library(session: Session) -> Library:
    shows = {s.id: s for s in session.scalars(select(Show))}
    meta = {m.show_id: m for m in session.scalars(select(ShowMeta))}
    counts, hours, seasons = {}, {}, {}
    rows = session.execute(
        select(
            Episode.show_id,
            func.count(Episode.id),
            func.sum(Episode.duration_ms),
            func.count(func.distinct(Episode.season)),
        )
        .where(Episode.season > 0, Episode.part_of_episode_id.is_(None))
        .group_by(Episode.show_id)
    )
    for show_id, n, ms, s in rows:
        counts[show_id], hours[show_id], seasons[show_id] = n, (ms or 0) / 3_600_000, s
    lib = Library(
        shows=shows,
        meta=meta,
        counts=counts,
        hours=hours,
        seasons=seasons,
        by_tmdb={s.tmdb_id: s.id for s in shows.values() if s.tmdb_id},
    )
    _build_vectors(lib)
    return lib


def _raw_features(show: Show, meta: ShowMeta | None) -> tuple[dict[str, float], dict[str, str]]:
    feats: dict[str, float] = {}
    labels: dict[str, str] = {}

    def add(kind: str, value: str | None, label: str | None = None):
        if not value:
            return
        key = f"{kind}:{value.lower()}"
        feats[key] = KIND_WEIGHT[kind]
        labels[key] = label or value

    if meta is not None:
        for g in norm_genres(meta.genres or []):
            add("g", g, genre_label(g))
        for k in (meta.keywords or [])[:25]:
            if k.lower() not in NOISE_KEYWORDS:
                add("k", k)
        for c in meta.creators or []:
            add("c", c)
        for n in meta.networks or ([meta.studio] if meta.studio else []):
            add("n", n)
        for a in (meta.cast or [])[:5]:
            add("a", a)
        add("r", rating_band(meta.content_rating))
        if meta.runtime_minutes:
            add("t", "half-hour" if meta.runtime_minutes <= 35 else "hour-long")
        add("o", (meta.countries or [None])[0])
        add("l", meta.language)
    if show.year:
        add("d", f"{show.year // 10 * 10}s")
    return feats, labels


def _build_vectors(lib: Library) -> None:
    raw = {}
    df: Counter[str] = Counter()
    for show_id, show in lib.shows.items():
        feats, labels = _raw_features(show, lib.meta.get(show_id))
        raw[show_id] = feats
        lib.labels.update(labels)
        df.update(feats.keys())
    n = len(raw) or 1
    for show_id, feats in raw.items():
        vec = {f: w * (math.log((n + 1) / (df[f] + 1)) + 0.2) for f, w in feats.items()}
        lib.vectors[show_id] = _normalize(vec)


def _normalize(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    return {k: v / norm for k, v in vec.items()} if norm else {}


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def watch_history(
    session: Session, settings: Settings, user_ids: list[int] | None = None
) -> dict[int, dict[int, Watch]]:
    """{user_id: {show_id: Watch}} from finished plays of regular episodes."""
    done = WatchEvent.percent_complete >= settings.watched_percent
    query = (
        select(
            WatchEvent.user_id,
            Episode.show_id,
            func.count(func.distinct(WatchEvent.episode_id)),
            func.count(WatchEvent.id),
            func.min(WatchEvent.watched_at),
            func.max(WatchEvent.watched_at),
            func.count(func.distinct(func.date(WatchEvent.watched_at))),
        )
        .join(Episode, Episode.id == WatchEvent.episode_id)
        .where(done, Episode.season > 0)
        .group_by(WatchEvent.user_id, Episode.show_id)
    )
    if user_ids is not None:
        query = query.where(WatchEvent.user_id.in_(user_ids))
    out: dict[int, dict[int, Watch]] = defaultdict(dict)
    for user_id, show_id, eps, plays, first, last, days in session.execute(query):
        out[user_id][show_id] = Watch(show_id, eps, plays, first, last, days)
    since = _now() - timedelta(days=14)
    recent = select(WatchEvent.user_id, Episode.show_id, func.count(func.distinct(Episode.id)))
    recent = (
        recent.join(Episode, Episode.id == WatchEvent.episode_id)
        .where(done, Episode.season > 0, WatchEvent.watched_at >= since)
        .group_by(WatchEvent.user_id, Episode.show_id)
    )
    for user_id, show_id, n in session.execute(recent):
        if show_id in out.get(user_id, {}):
            out[user_id][show_id].recent = n
    return out


def preferences(session: Session, user_id: int) -> dict[str, set[str]]:
    """{"show:12": {"love"}, "tmdb:1396": {"later"}}"""
    out: dict[str, set[str]] = defaultdict(set)
    for p in session.scalars(select(Preference).where(Preference.user_id == user_id)):
        out[f"show:{p.show_id}" if p.show_id else f"tmdb:{p.tmdb_id}"].add(p.kind)
    return out


def classify(
    watch: Watch, lib: Library, progress: WatchProgress | None, now: datetime, prefs: set[str]
) -> None:
    """Set state, affinity and weight for one viewer's history with one show."""
    total = lib.counts.get(watch.show_id, 0)
    age = (now - watch.last).days
    fraction = min(1.0, watch.eps / total) if total else 1.0
    has_next = _has_next(lib, watch, progress)
    if not has_next and watch.eps:
        watch.state = "finished"
    elif watch.eps <= 2 and total > 3 and age > SAMPLED_DAYS:
        watch.state = "sampled"
    elif age <= WATCHING_DAYS:
        watch.state = "watching"
    else:
        watch.state = "paused"

    pace = min(1.0, watch.pace / 2)
    a = {
        "finished": 0.8 + 0.2 * pace,
        "watching": 0.4 + 0.3 * min(1.0, fraction * 2) + 0.2 * pace,
        "paused": 0.1 + 0.6 * fraction if fraction >= 0.15 else -0.15,
        "sampled": -0.4 if watch.eps <= 1 else -0.25,
    }[watch.state]
    if watch.plays > watch.eps * 1.3 and watch.eps >= 3:
        a += 0.2  # rewatched
    if "love" in prefs:
        a = 1.0
    elif "nope" in prefs:
        a = -1.0
    watch.affinity = max(-1.0, min(1.0, a))
    watch.weight = max(0.3, 0.5 ** (age / 540))


def _has_next(lib: Library, watch: Watch, progress: WatchProgress | None) -> bool:
    if progress is None or progress.last_watched_episode_id is None:
        return watch.eps < lib.counts.get(watch.show_id, 0)
    return progress.last_watched_episode_id not in lib.last.get(watch.show_id, set())


def _load_last_episodes(session: Session, lib: Library, show_ids: set[int]) -> None:
    """For each show, the id(s) of its final regular episode in the library."""
    last = lib.last
    wanted = [s for s in show_ids if s not in last]
    if not wanted:
        return
    tops = (
        select(Episode.show_id, func.max(Episode.season * 10000 + Episode.episode).label("k"))
        .where(Episode.show_id.in_(wanted), Episode.season > 0)
        .group_by(Episode.show_id)
        .subquery()
    )
    rows = session.execute(
        select(Episode.show_id, Episode.id, Episode.part_of_episode_id).join(
            tops,
            (tops.c.show_id == Episode.show_id)
            & (Episode.season * 10000 + Episode.episode == tops.c.k),
        )
    )
    for show_id, ep_id, part_of in rows:
        last.setdefault(show_id, set()).update({ep_id} | ({part_of} if part_of else set()))


# ---- a viewer's taste ------------------------------------------------------------------


@dataclass
class Profile:
    user: User
    watches: dict[int, Watch]
    prefs: dict[str, set[str]]
    taste: dict[str, float]
    contrib: dict[str, list[tuple[float, int]]]  # feature -> [(weight, show_id)] behind it


def build_profile(
    session: Session,
    lib: Library,
    user: User,
    watches: dict[int, Watch],
    now: datetime,
) -> Profile:
    prefs = preferences(session, user.id)
    progress = {
        p.show_id: p
        for p in session.scalars(select(WatchProgress).where(WatchProgress.user_id == user.id))
    }
    _load_last_episodes(session, lib, set(watches))
    for show_id, w in watches.items():
        classify(w, lib, progress.get(show_id), now, prefs.get(f"show:{show_id}", set()))
    # Marked "seen elsewhere" or "love" without a history here: still says something.
    for key, kinds in prefs.items():
        if key.startswith("show:") and kinds & {"love", "seen", "nope"}:
            show_id = int(key[5:])
            if show_id not in watches and show_id in lib.shows:
                a = 1.0 if "love" in kinds else -1.0 if "nope" in kinds else 0.6
                watches[show_id] = Watch(show_id, 0, 0, now, now, 0, 0, "marked", a, 1.0)
    taste: dict[str, float] = defaultdict(float)
    contrib: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for show_id, w in watches.items():
        weight = w.affinity * w.weight
        if abs(weight) < 0.05:
            continue
        for f, v in lib.vectors.get(show_id, {}).items():
            taste[f] += weight * v
            if weight > 0:
                contrib[f].append((weight * v, show_id))
    return Profile(user, watches, prefs, _normalize(dict(taste)), contrib)


# ---- scoring ---------------------------------------------------------------------------


@dataclass
class Candidate:
    key: str
    show_id: int | None = None
    tmdb_id: int | None = None
    parts: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    reasons: list[dict] = field(default_factory=list)
    vector: dict[str, float] = field(default_factory=dict)


def quality(vote_average: float | None, vote_count: int | None, plex_rating: float | None) -> float:
    """0..1 from TMDB's rating, pulled towards average when few people voted."""
    if vote_average and vote_count:
        v, m, c = vote_count, 150, 6.9
        q = v / (v + m) * vote_average + m / (v + m) * c
    elif plex_rating:
        q = 50 / 200 * plex_rating + 150 / 200 * 6.9
    else:
        return 0.3
    return max(0.0, min(1.0, (q - 5.5) / 3.0))


def related_scores(
    lib: Library, profile: Profile
) -> tuple[dict[int, float], dict[int, list[tuple[float, int]]]]:
    """TMDB id -> how strongly TMDB ties it to shows this viewer liked, and which shows."""
    raw: dict[int, float] = defaultdict(float)
    who: dict[int, list[tuple[float, int]]] = defaultdict(list)
    liked = {s: w for s, w in profile.watches.items() if w.affinity > 0.3}
    for show_id, w in liked.items():
        meta = lib.meta.get(show_id)
        for r in meta.related if meta else []:
            strength = (
                w.affinity * w.weight * (1 - r["rank"] / 25) * (1.0 if r["kind"] == "rec" else 0.6)
            )
            raw[r["tmdb_id"]] += strength
            who[r["tmdb_id"]].append((strength, show_id))
    # The other direction: a candidate whose own related list includes shows you liked.
    liked_tmdb = {lib.shows[s].tmdb_id: s for s in liked if lib.shows[s].tmdb_id}
    for show_id, meta in lib.meta.items():
        tmdb_id = lib.shows[show_id].tmdb_id if show_id in lib.shows else None
        if not tmdb_id or show_id in profile.watches:
            continue
        for r in meta.related or []:
            source = liked_tmdb.get(r["tmdb_id"])
            if source is None:
                continue
            w = liked[source]
            strength = 0.7 * w.affinity * w.weight * (1 - r["rank"] / 25)
            raw[tmdb_id] += strength
            who[tmdb_id].append((strength, source))
    scores = {t: 1 - math.exp(-v / 1.2) for t, v in raw.items()}
    return scores, who


def household_scores(
    lib: Library, profile: Profile, others: dict[int, tuple[str, dict[int, Watch]]]
) -> dict[int, tuple[float, str]]:
    """show_id -> (strength, reason) from what other viewers here finished or are hooked on."""
    out: dict[int, tuple[float, str]] = {}
    for _uid, (name, watches) in others.items():
        for show_id, w in watches.items():
            if w.state == "finished" and w.affinity >= 0.8:
                s, text = w.affinity * 0.8, f"{name} finished it"
                if w.pace >= 2:
                    days = plural(w.days, "day")
                    text = f"{name} binged it ({w.eps} episodes in {days})"
            elif w.state == "watching" and w.recent >= 3:
                s, text = 0.6, f"{name} is hooked ({w.recent} episodes in two weeks)"
            else:
                continue
            if s > out.get(show_id, (0, ""))[0]:
                out[show_id] = (s, text)
    return out


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _titles(lib: Library, show_ids: list[int]) -> str:
    names = [lib.shows[s].title for s in show_ids if s in lib.shows]
    return " and ".join(names[:2])


def _verb(w: Watch, lib: Library | None = None) -> str:
    if w.state == "finished" and lib is not None:
        meta = lib.meta.get(w.show_id)
        if meta is not None and status_label(meta.tmdb_status) == "Ongoing":
            return "are caught up on"
    return {
        "finished": "finished",
        "watching": "are watching",
        "paused": "watched",
        "marked": "liked",
    }.get(w.state, "watched")


def explain(
    lib: Library,
    profile: Profile,
    cand: Candidate,
    related_who: list[tuple[float, int]],
    household: tuple[float, str] | None,
    meta: ShowMeta | None,
    fresh_days: int | None,
) -> list[dict]:
    reasons: list[tuple[float, str, str]] = []
    if related_who:
        top: list[int] = []
        for _, s in sorted(related_who, reverse=True):
            if s not in top:
                top.append(s)
        first = profile.watches[top[0]]
        verb = (
            "loved" if "love" in profile.prefs.get(f"show:{top[0]}", set()) else _verb(first, lib)
        )
        names = _titles(lib, top[:2])
        reasons.append(
            (
                cand.parts.get("related", 0) + 0.5,
                "related",
                f"Because you {verb} {names}".replace("you are", "you're"),
            )
        )
    if meta is not None and cand.show_id is not None:
        liked = [s for s, w in profile.watches.items() if w.affinity > 0.5 and s in lib.meta]
        for creator in meta.creators or []:
            made = [s for s in liked if creator in (lib.meta[s].creators or [])]
            if made:
                reasons.append(
                    (0.6, "creator", f"From {creator}, who made {lib.shows[made[0]].title}")
                )
                break
        for actor in (meta.cast or [])[:4]:
            was_in = [s for s in liked if actor in (lib.meta[s].cast or [])[:6]]
            if was_in:
                reasons.append((0.35, "cast", f"{actor}, from {lib.shows[was_in[0]].title}"))
                break
    tags = sorted(
        (
            (profile.taste.get(f, 0) * v, f)
            for f, v in cand.vector.items()
            if f[0] in "gk" and profile.taste.get(f, 0) > 0
        ),
        reverse=True,
    )[:3]
    if tags and cand.parts.get("taste", 0) > 0.25:
        words = ", ".join(lib.labels.get(f, f[2:]).lower() for _, f in tags)
        reasons.append((cand.parts["taste"], "taste", f"Your kind of show: {words}"))
    if household:
        reasons.append((household[0], "household", household[1]))
    if (
        meta is not None
        and meta.vote_average
        and (meta.vote_count or 0) >= 200
        and meta.vote_average >= 7.8
    ):
        reasons.append((0.2, "quality", f"Rated {meta.vote_average:.1f} on TMDB"))
    if fresh_days is not None and fresh_days <= 30:
        when = "this week" if fresh_days <= 7 else f"{fresh_days} days ago"
        reasons.append((0.15, "fresh", f"Added to Plex {when}"))
    reasons.sort(reverse=True)
    return [{"kind": k, "text": t} for _, k, t in reasons[:4]]


def score_library(
    lib: Library,
    profile: Profile,
    others: dict[int, tuple[str, dict[int, Watch]]],
    now: datetime,
    include: set[int] | None = None,
) -> dict[int, Candidate]:
    """Score every library show this viewer hasn't started (plus `include`)."""
    related, related_who = related_scores(lib, profile)
    house = household_scores(lib, profile, others)
    pool = [
        s
        for s in lib.shows
        if lib.counts.get(s)
        and (s not in profile.watches or (include and s in include))
        and "nope" not in profile.prefs.get(f"show:{s}", set())
    ]
    raw_taste = {s: _dot(profile.taste, lib.vectors.get(s, {})) for s in pool}
    positives = sorted(v for v in raw_taste.values() if v > 0)
    ref = positives[int(len(positives) * 0.9)] if positives else 1.0
    ref = max(ref, 0.05)
    out: dict[int, Candidate] = {}
    for s in pool:
        show, meta = lib.shows[s], lib.meta.get(s)
        tmdb = show.tmdb_id
        fresh_days = (now - meta.added_at).days if meta and meta.added_at else None
        parts = {
            "taste": max(-1.0, min(1.0, raw_taste[s] / ref)),
            "related": related.get(tmdb, 0.0) if tmdb else 0.0,
            "quality": quality(
                meta.vote_average if meta else None,
                meta.vote_count if meta else None,
                meta.audience_rating if meta else None,
            ),
            "household": house.get(s, (0.0, ""))[0],
            "fresh": 1.0
            if fresh_days is not None and fresh_days <= 21
            else 0.4
            if fresh_days is not None and fresh_days <= 60
            else 0.0,
        }
        cand = Candidate(key=f"show:{s}", show_id=s, tmdb_id=tmdb, parts=parts)
        cand.score = sum(SCORE_WEIGHTS[k] * v for k, v in parts.items())
        if "later" in profile.prefs.get(cand.key, set()):
            cand.score += 0.05
        cand.vector = lib.vectors.get(s, {})
        cand.reasons = explain(
            lib,
            profile,
            cand,
            related_who.get(tmdb, []) if tmdb else [],
            house.get(s),
            meta,
            fresh_days,
        )
        out[s] = cand
    return out


def diversify(cands: list[Candidate], n: int, penalty: float = 0.25) -> list[Candidate]:
    """Greedy re-rank: each pick is the best score minus its similarity to what's picked."""
    pool = sorted(cands, key=lambda c: c.score, reverse=True)[: n * 4]
    picked: list[Candidate] = []
    while pool and len(picked) < n:
        best = max(
            pool,
            key=lambda c: (
                c.score - penalty * max((_dot(c.vector, p.vector) for p in picked), default=0)
            ),
        )
        picked.append(best)
        pool.remove(best)
    return picked


# ---- filters ---------------------------------------------------------------------------


@dataclass
class Filters:
    time: str = "any"  # any, quick (short episodes), weekend (<= 15 h), long (40 h+)
    story: str = "any"  # any, complete (ended), ongoing
    genre: str = ""  # a normalized genre, e.g. "comedy"


def status_label(status: str | None) -> str | None:
    if not status:
        return None
    return {
        "Ended": "Complete story",
        "Canceled": "Canceled",
        "Returning Series": "Ongoing",
        "In Production": "Ongoing",
        "Planned": "Ongoing",
        "Pilot": "Ongoing",
    }.get(status, status)


def passes(lib: Library, show_id: int, f: Filters) -> bool:
    meta = lib.meta.get(show_id)
    if f.genre and f.genre not in norm_genres(meta.genres if meta else []):
        return False
    if f.story == "complete" and not (meta and meta.tmdb_status == "Ended"):
        return False
    if f.story == "ongoing" and not (meta and status_label(meta.tmdb_status) == "Ongoing"):
        return False
    hours = lib.hours.get(show_id, 0)
    runtime = meta.runtime_minutes if meta and meta.runtime_minutes else None
    if f.time == "quick" and not (runtime and runtime <= 35):
        return False
    if f.time == "weekend" and not (0 < hours <= 15):
        return False
    return not (f.time == "long" and hours < 40)


# ---- the page --------------------------------------------------------------------------


def play_url(machine_id: str | None, rating_key: str | None) -> str | None:
    if not machine_id or not rating_key:
        return None
    key = quote(f"/library/metadata/{rating_key}", safe="")
    return f"https://app.plex.tv/desktop/#!/server/{machine_id}/details?key={key}"


def _facts(lib: Library, show_id: int) -> dict:
    meta = lib.meta.get(show_id)
    have = lib.counts.get(show_id, 0)
    aired = meta.episodes_total if meta else None
    return {
        "seasons": (meta.seasons_total if meta and meta.seasons_total else None)
        or lib.seasons.get(show_id),
        "episodes": have,
        "runtime": meta.runtime_minutes if meta else None,
        "hours": round(lib.hours.get(show_id, 0), 1),
        "status": status_label(meta.tmdb_status if meta else None),
        "genres": [genre_label(g) for g in norm_genres(meta.genres if meta else [])][:3],
        "rating": round(meta.vote_average, 1) if meta and meta.vote_average else None,
        "content_rating": meta.content_rating if meta else None,
        "missing": max(0, aired - have) if aired and have < aired * 0.85 else 0,
        "aired": aired,
    }


def show_item(lib: Library, show_id: int, prefs: dict[str, set[str]], machine: str | None) -> dict:
    show, meta = lib.shows[show_id], lib.meta.get(show_id)
    key = f"show:{show_id}"
    return {
        "key": key,
        "show_id": show_id,
        "tmdb_id": show.tmdb_id,
        "title": show.title,
        "year": show.year,
        "poster": f"/api/shows/{show_id}/poster" if show.poster_url else None,
        "art": f"/api/shows/{show_id}/art" if meta and meta.art_url else None,
        "colors": meta.colors if meta else [],
        "overview": (meta.summary if meta else None),
        "tagline": meta.tagline if meta else None,
        "facts": _facts(lib, show_id),
        "prefs": sorted(prefs.get(key, set())),
        "play_url": play_url(machine, show.plex_rating_key),
        "in_library": True,
    }


def _warnings(facts: dict) -> list[str]:
    out = []
    if facts.get("status") == "Canceled":
        out.append("Canceled, so it may end without wrapping up")
    if facts.get("missing") and facts.get("status") in ("Complete story", "Canceled"):
        out.append(f"Plex has {facts['episodes']} of {facts['aired']} episodes")
    return out


def candidate_item(
    lib: Library, cand: Candidate, prefs: dict[str, set[str]], machine: str | None, rank: dict
) -> dict:
    item = show_item(lib, cand.show_id, prefs, machine)
    item.update(
        score=round(cand.score, 4),
        match=rank.get(cand.key),
        parts={k: round(v, 3) for k, v in cand.parts.items()},
        reasons=cand.reasons,
        warnings=_warnings(item["facts"]),
    )
    return item


def _percentiles(cands: list[Candidate]) -> dict[str, int]:
    """Candidate key -> how much of the library it beats, as a percentage."""
    ordered = sorted(cands, key=lambda c: c.score)
    n = len(ordered)
    return {c.key: round(100 * i / max(1, n - 1)) for i, c in enumerate(ordered)}


def _episodes_after(session: Session, show_id: int, boundary: Episode | None) -> list[Episode]:
    query = select(Episode).where(
        Episode.show_id == show_id, Episode.season > 0, Episode.part_of_episode_id.is_(None)
    )
    eps = list(session.scalars(query.order_by(Episode.season, Episode.episode)))
    if boundary is None:
        return eps
    return [e for e in eps if e.order_key > boundary.order_key]


def progress_item(
    session: Session,
    lib: Library,
    w: Watch,
    user: User,
    settings: Settings,
    prefs: dict[str, set[str]],
    machine: str | None,
    now: datetime,
) -> dict | None:
    progress = session.scalar(
        select(WatchProgress).where(
            WatchProgress.user_id == user.id, WatchProgress.show_id == w.show_id
        )
    )
    boundary = (
        session.get(Episode, progress.last_watched_episode_id)
        if progress and progress.last_watched_episode_id
        else None
    )
    ahead = _episodes_after(session, w.show_id, boundary)
    if not ahead:
        return None
    nxt = ahead[0]
    in_season = [e for e in ahead if e.season == nxt.season]
    last_day = w.last.date().isoformat()
    new = [e for e in ahead if e.air_date and e.air_date > last_day]
    days = (now - w.last).days
    gap = user.gap_threshold_days or settings.default_gap_days
    pace = w.recent / 14 if w.recent else w.pace / 7  # episodes a day, lately
    left_minutes = sum((e.duration_ms or 0) for e in ahead) / 60000
    item = show_item(lib, w.show_id, prefs, machine)
    item.update(
        progress={
            # By position: earlier episodes may have been watched before Tautulli kept history
            "watched": max(0, lib.counts.get(w.show_id, 0) - len(ahead)),
            "total": lib.counts.get(w.show_id, 0),
            "last_code": boundary.code if boundary else None,
            "next_code": nxt.code,
            "next_title": nxt.title,
            "next_minutes": round((nxt.duration_ms or 0) / 60000) or None,
            "days_since": days,
            "left_in_season": len(in_season),
            "season_minutes": round(sum((e.duration_ms or 0) for e in in_season) / 60000),
            "left_minutes": round(left_minutes),
            "finish_days": math.ceil(len(ahead) / pace) if pace > 0 and len(ahead) else None,
            "new_count": len(new),
            "new_season": bool(new) and nxt.episode == 1 and len(new) == len(ahead),
            "all_new": bool(new) and len(new) == len(ahead),
            "recap": days >= gap and w.eps > 0,
            "state": w.state,
        },
        play_url=play_url(machine, nxt.plex_rating_key) or item["play_url"],
    )
    return item


def momentum(item: dict, w: Watch) -> float:
    p = item["progress"]
    return (
        0.55 * math.exp(-p["days_since"] / 14)
        + 0.25 * min(1.0, w.recent / 6)
        + 0.2 * max(0.0, w.affinity)
    )


def tmdb_item(t: TmdbTitle, score: float, reasons: list[dict], prefs, in_sonarr: bool) -> dict:
    key = f"tmdb:{t.tmdb_id}"
    return {
        "key": key,
        "show_id": None,
        "tmdb_id": t.tmdb_id,
        "title": t.name,
        "year": t.year,
        "poster": f"/api/tmdb/image/w342{t.poster_path}" if t.poster_path else None,
        "art": f"/api/tmdb/image/w780{t.backdrop_path}" if t.backdrop_path else None,
        "colors": [],
        "overview": t.overview,
        "tagline": None,
        "facts": {
            "genres": [genre_label(g) for g in norm_genres(t.genres or [])][:3],
            "rating": round(t.vote_average, 1) if t.vote_average else None,
        },
        "prefs": sorted(prefs.get(key, set())),
        "play_url": None,
        "in_library": False,
        "in_sonarr": in_sonarr,
        "score": round(score, 4),
        "reasons": reasons,
        "warnings": [],
    }


def taste_summary(lib: Library, profile: Profile) -> dict:
    tags = []
    for f, v in sorted(profile.taste.items(), key=lambda kv: kv[1], reverse=True):
        if v <= 0 or f[0] not in "gkcna":
            continue
        tags.append({"kind": f[0], "label": lib.labels.get(f, f[2:]), "weight": round(v, 3)})
        if len(tags) == 12:
            break
    states = Counter(w.state for w in profile.watches.values() if w.state != "marked")
    tried = states["finished"] + states["sampled"] + states["paused"]
    finished = [w for w in profile.watches.values() if w.state == "finished" and w.days]
    dropped: Counter[str] = Counter()
    for w in profile.watches.values():
        if w.state == "sampled" and w.show_id in lib.meta:
            dropped.update(norm_genres(lib.meta[w.show_id].genres or []))
    paces = sorted(w.pace for w in finished)
    return {
        "tags": tags,
        "shows": sum(states.values()),
        "finished": states["finished"],
        "watching": states["watching"],
        "dropped": states["sampled"],
        "finish_rate": round(100 * states["finished"] / tried) if tried else None,
        "pace": round(paces[len(paces) // 2], 1) if paces else None,
        "drops_genre": genre_label(dropped.most_common(1)[0][0]) if dropped else None,
    }


def genre_options(lib: Library) -> list[dict]:
    counts: Counter[str] = Counter()
    for m in lib.meta.values():
        counts.update(norm_genres(m.genres or []))
    return [{"value": g, "label": genre_label(g), "count": n} for g, n in counts.most_common(16)]


@dataclass
class Context:
    session: Session
    settings: Settings
    lib: Library
    now: datetime
    history: dict[int, dict[int, Watch]]
    users: dict[int, User]
    machine: str | None
    sonarr_tmdb: set[int] = field(default_factory=set)
    sonarr_tvdb: set[int] = field(default_factory=set)
    profiles: dict[int, Profile] = field(default_factory=dict)
    names: bool = True  # name other viewers in reasons ("Sam finished it")

    def profile(self, user_id: int) -> Profile:
        cache = self.profiles
        if user_id not in cache:
            watches = {s: Watch(**vars(w)) for s, w in self.history.get(user_id, {}).items()}
            cache[user_id] = build_profile(
                self.session, self.lib, self.users[user_id], watches, self.now
            )
        return cache[user_id]

    def others(self, user_id: int) -> dict[int, tuple[str, dict[int, Watch]]]:
        """Everyone else's history, by name (or anonymously, for viewers who aren't admins)."""
        return {
            uid: (
                self.users[uid].display_name if self.names else "Someone here",
                self.profile(uid).watches,
            )
            for uid in self.history
            if uid != user_id and uid in self.users
        }


def make_context(
    session: Session, settings: Settings, sonarr: tuple[set[int], set[int]] | None = None
) -> Context:
    now = _now()
    ctx = Context(
        session=session,
        settings=settings,
        lib=load_library(session),
        now=now,
        history=watch_history(session, settings),
        users={u.id: u for u in session.scalars(select(User))},
        machine=plex_machine_id(session),
    )
    if sonarr:
        ctx.sonarr_tmdb, ctx.sonarr_tvdb = sonarr
    return ctx


def up_next(ctx: Context, user_id: int, filters: Filters, with_user: int | None = None) -> dict:
    lib, now = ctx.lib, ctx.now
    profile = ctx.profile(user_id)
    user = profile.user
    prefs = profile.prefs

    # What they're in the middle of
    keep, back, new_eps = [], [], []
    for show_id, w in profile.watches.items():
        if w.state not in ("watching", "paused") or show_id not in lib.shows:
            continue
        if "nope" in prefs.get(f"show:{show_id}", set()):
            continue
        item = progress_item(ctx.session, lib, w, user, ctx.settings, prefs, ctx.machine, now)
        if item is None:
            continue
        p = item["progress"]
        item["momentum"] = round(momentum(item, w), 3)
        if p["all_new"] and w.eps >= 3:
            n = p["new_count"]
            what = (
                f"Season {p['next_code'][1:3].lstrip('0')} is here"
                if p["new_season"]
                else f"{plural(n, 'new episode')}"
            )
            item["reasons"] = [{"kind": "new", "text": f"{what} · you were caught up"}]
            new_eps.append(item)
        elif w.state == "watching":
            item["reasons"] = _keep_reasons(p, w)
            keep.append(item)
        elif w.eps >= 3 and w.affinity > 0.25:
            item["reasons"] = _back_reasons(p, w)
            back.append(item)
    keep.sort(key=lambda i: i["momentum"], reverse=True)
    back.sort(
        key=lambda i: (profile.watches[i["show_id"]].affinity, -i["progress"]["days_since"]),
        reverse=True,
    )
    new_eps.sort(key=lambda i: i["progress"]["days_since"])

    # Something new
    sampled = {s for s, w in profile.watches.items() if w.state == "sampled"}
    scored = score_library(lib, profile, ctx.others(user_id), now, include=sampled)
    rank = _percentiles([c for s, c in scored.items() if s not in sampled])
    fresh = [c for s, c in scored.items() if s not in sampled and passes(lib, s, filters)]
    start = [candidate_item(lib, c, prefs, ctx.machine, rank) for c in diversify(fresh, LANE_SIZE)]
    another = sorted(
        (c for s, c in scored.items() if s in sampled and passes(lib, s, filters)),
        key=lambda c: c.score,
        reverse=True,
    )
    retry = []
    for c in another[:12]:
        if c.score < 0.3:
            break
        item = candidate_item(lib, c, prefs, ctx.machine, rank)
        w = profile.watches[c.show_id]
        tried = f"You tried {plural(w.eps, 'episode')} in {w.last.strftime('%b %Y')}"
        item["reasons"] = [{"kind": "retry", "text": tried}, *item["reasons"][:2]]
        retry.append(item)
    saved = []
    for k, kinds in prefs.items():
        if "later" not in kinds or not k.startswith("show:") or int(k[5:]) not in lib.shows:
            continue
        show_id = int(k[5:])
        if show_id in scored:
            saved.append(candidate_item(lib, scored[show_id], prefs, ctx.machine, rank))
        else:  # loved or seen elsewhere, so not scored, but still on the list
            item = show_item(lib, show_id, prefs, ctx.machine)
            item.update(reasons=[{"kind": "later", "text": "On your list"}], warnings=[])
            saved.append(item)

    lanes = [
        _lane("keep", "Keep going", "In the middle of these", keep),
        _lane("new", "New episodes", "Shows you'd caught up on have more", new_eps),
        _lane("list", "Your list", "Saved for later", saved),
        _lane("start", "Start something new", "From your Plex library, picked for you", start),
        _lane("back", "Pick back up", "You were into these, then stopped", back),
        _lane(
            "retry", "Give it another go", "You tried these, but they fit what you like now", retry
        ),
    ]
    if with_user and with_user != user_id and with_user in ctx.users:
        lanes.insert(1, together_lane(ctx, user_id, with_user, filters))
    lanes.append(beyond_lane(ctx, profile, filters))
    lanes = [lane for lane in lanes if lane["items"]]
    tonight = _tonight(keep, new_eps, start)
    return {
        "user": {"id": user.id, "name": user.display_name},
        "tonight": tonight,
        "lanes": lanes,
        "taste": taste_summary(lib, profile),
        "genres": genre_options(lib),
        "filters": vars(filters),
        "status": {
            "shows": len(lib.shows),
            "with_details": sum(1 for m in lib.meta.values() if m.plex_fetched_at),
            "with_tmdb": sum(1 for m in lib.meta.values() if m.tmdb_fetched_at),
            "tmdb": bool(ctx.settings.tmdb_api_key),
            "sonarr": bool(ctx.settings.sonarr_url and ctx.settings.sonarr_api_key),
        },
    }


def _lane(key: str, title: str, subtitle: str, items: list[dict]) -> dict:
    return {"key": key, "title": title, "subtitle": subtitle, "items": items[:LANE_SIZE]}


def _keep_reasons(p: dict, w: Watch) -> list[dict]:
    out = []
    if p["recap"]:
        out.append(
            {"kind": "recap", "text": f"{plural(p['days_since'], 'day')} away: a recap first?"}
        )
    if w.recent >= 3:
        out.append({"kind": "pace", "text": f"{w.recent} episodes in the last two weeks"})
    left = p["left_in_season"]
    if left <= 3:
        out.append({"kind": "season", "text": f"{plural(left, 'episode')} left in the season"})
    if p["finish_days"] and p["finish_days"] <= 60:
        out.append(
            {
                "kind": "finish",
                "text": f"At your pace, done in about {plural(p['finish_days'], 'day')}",
            }
        )
    return out[:3]


def _back_reasons(p: dict, w: Watch) -> list[dict]:
    left = p["left_in_season"]
    out = [
        {"kind": "back", "text": f"You were {plural(left, 'episode')} from the end of the season"}
    ]
    if p["recap"]:
        out.append(
            {"kind": "recap", "text": f"{w.last.strftime('%b %Y')}: a recap gets you back in"}
        )
    return out


def _tonight(keep: list[dict], new_eps: list[dict], start: list[dict]) -> dict | None:
    """One pick for right now, and a few others if that one doesn't suit."""
    order: list[tuple[str, dict]] = []
    if keep and keep[0]["momentum"] >= 0.45:
        order.append(("keep", keep[0]))
    order += [("new", i) for i in new_eps[:2]]
    order += [("keep", i) for i in keep[1:3] if i["momentum"] >= 0.3]
    order += [("start", i) for i in start[:4]]
    if keep and ("keep", keep[0]) not in order:
        order.append(("keep", keep[0]))
    if not order:
        return None
    picks = [{"lane": lane, **item} for lane, item in order[:6]]
    return {"pick": picks[0], "others": picks[1:]}


def together_lane(ctx: Context, user_id: int, other_id: int, filters: Filters) -> dict:
    lib, now = ctx.lib, ctx.now
    me, them = ctx.profile(user_id), ctx.profile(other_id)
    name = them.user.display_name
    items: list[dict] = []
    # Shows you're both partway through
    for show_id in set(me.watches) & set(them.watches):
        a, b = me.watches[show_id], them.watches[show_id]
        if a.state not in ("watching", "paused") and b.state not in ("watching", "paused"):
            continue
        if a.state == "sampled" or b.state == "sampled":
            continue
        pos = _positions(ctx.session, (user_id, other_id), show_id)
        if pos is None:
            continue
        mine, theirs, minutes_between = pos
        gap = abs(mine["idx"] - theirs["idx"])
        recent = min((now - a.last).days, (now - b.last).days)
        if gap > TOGETHER_MAX_GAP or recent > 120:
            continue  # too far apart to watch together, or nobody's watching it lately
        item = show_item(lib, show_id, me.prefs, ctx.machine)
        catch_up = _hm(minutes_between)
        if gap == 0:
            text = f"You're both on {mine['code']}"
        elif mine["idx"] > theirs["idx"]:
            text = f"You're {plural(gap, 'episode')} ahead of {name} ({catch_up} to catch them up)"
        else:
            text = f"{name} is {plural(gap, 'episode')} ahead ({catch_up} for you to catch up)"
        item["reasons"] = [{"kind": "together", "text": text}]
        item["together"] = {"mine": mine["code"], "theirs": theirs["code"]}
        item["score"] = 2 + 1 / (1 + gap) + max(0.0, 1 - recent / 120)
        items.append(item)
    items.sort(key=lambda i: i["score"], reverse=True)
    # Something new for both: fair to both, so the weaker of the two matters most
    mine_s = score_library(lib, me, ctx.others(user_id), now)
    theirs_s = score_library(lib, them, ctx.others(other_id), now)
    pair = []
    for s, c in mine_s.items():
        o = theirs_s.get(s)
        if o is None or not passes(lib, s, filters) or c.score <= 0 or o.score <= 0:
            continue
        both = Candidate(key=c.key, show_id=s, tmdb_id=c.tmdb_id, vector=c.vector)
        both.score = 2 * c.score * o.score / (c.score + o.score)
        both.parts = {k: round((c.parts[k] + o.parts[k]) / 2, 3) for k in c.parts}
        theirs_reason = next(
            (r for r in o.reasons if r["kind"] in ("related", "creator", "taste")), None
        )
        both.reasons = [
            *(r for r in c.reasons[:1]),
            *(
                [
                    {
                        "kind": "together",
                        "text": f"For {name}: " + _second_person(theirs_reason["text"]),
                    }
                ]
                if theirs_reason
                else []
            ),
        ]
        pair.append(both)
    rank = _percentiles(pair)
    for c in diversify(pair, 12):
        items.append(candidate_item(lib, c, me.prefs, ctx.machine, rank))
    return _lane(
        "together",
        f"Watch with {name}",
        "Shows you're both on, and new ones you'd both like",
        items,
    )


def _second_person(text: str) -> str:
    """'Because you finished X' -> 'finished X', for a "For Sam: ..." line."""
    return (
        text.replace("Because you're ", "")
        .replace("Because you ", "")
        .replace("Your kind of show", "their kind of show")
    )


def _hm(minutes: float) -> str:
    minutes = round(minutes)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} m" if minutes % 60 else f"{minutes // 60} h"


def _positions(session: Session, users: tuple[int, int], show_id: int):
    eps = [
        e
        for e in session.scalars(
            select(Episode)
            .where(
                Episode.show_id == show_id, Episode.season > 0, Episode.part_of_episode_id.is_(None)
            )
            .order_by(Episode.season, Episode.episode)
        )
    ]
    index = {e.id: i for i, e in enumerate(eps)}
    out = []
    for uid in users:
        p = session.scalar(
            select(WatchProgress).where(
                WatchProgress.user_id == uid, WatchProgress.show_id == show_id
            )
        )
        if p is None or p.last_watched_episode_id not in index:
            return None
        i = index[p.last_watched_episode_id]
        out.append({"idx": i, "code": eps[i].code})
    lo, hi = sorted((out[0]["idx"], out[1]["idx"]))
    minutes = sum((e.duration_ms or 0) for e in eps[lo + 1 : hi + 1]) / 60000
    return out[0], out[1], minutes


def language_shares(lib: Library, profile: Profile) -> dict[str, float]:
    """Original language -> how much of what this viewer liked is in it, relative to the most."""
    weights: Counter[str] = Counter()
    for show_id, w in profile.watches.items():
        meta = lib.meta.get(show_id)
        if w.affinity > 0.3 and meta is not None and meta.language:
            weights[meta.language] += w.affinity * w.weight
    top = max(weights.values(), default=0)
    return {lang: v / top for lang, v in weights.items()} if top else {}


def beyond_lane(ctx: Context, profile: Profile, filters: Filters) -> dict:
    """TMDB's picks for this viewer that aren't in Plex."""
    related, who = related_scores(ctx.lib, profile)
    ids = [t for t in related if t not in ctx.lib.by_tmdb]
    titles = (
        {
            t.tmdb_id: t
            for t in ctx.session.scalars(select(TmdbTitle).where(TmdbTitle.tmdb_id.in_(ids)))
        }
        if ids
        else {}
    )
    taste_genres = {f[2:]: v for f, v in profile.taste.items() if f.startswith("g:") and v > 0}
    languages = language_shares(ctx.lib, profile)
    items = []
    for t_id, g in sorted(related.items(), key=lambda kv: kv[1], reverse=True)[:200]:
        t = titles.get(t_id)
        key = f"tmdb:{t_id}"
        if t is None or "nope" in profile.prefs.get(key, set()) or (t.vote_count or 0) < 25:
            continue
        genres = norm_genres(t.genres or [])
        if filters.genre and filters.genre not in genres:
            continue
        fit = sum(taste_genres.get(x, 0) for x in genres)
        score = (
            0.65 * g + 0.25 * quality(t.vote_average, t.vote_count, None) + 0.1 * min(1.0, fit * 2)
        )
        # Someone who only watches English-language TV rarely wants a Korean drama, and the
        # other way round; how much they watch in a language is the best guess there is.
        score *= 0.35 + 0.65 * languages.get(t.language or "", 0.0)
        top = []
        for _, s in sorted(who.get(t_id, []), reverse=True):
            if s not in top:
                top.append(s)
        reasons = []
        if top:
            verb = _verb(profile.watches[top[0]], ctx.lib)
            because = f"Because you {verb} {_titles(ctx.lib, top[:2])}".replace("you are", "you're")
            reasons.append({"kind": "related", "text": because})
        if t.vote_average and (t.vote_count or 0) >= 200 and t.vote_average >= 7.8:
            reasons.append({"kind": "quality", "text": f"Rated {t.vote_average:.1f} on TMDB"})
        in_sonarr = t_id in ctx.sonarr_tmdb
        items.append(tmdb_item(t, score, reasons, profile.prefs, in_sonarr))
    items.sort(key=lambda i: i["score"], reverse=True)
    return _lane(
        "beyond", "Not in your library", "What TMDB would add, based on what you liked", items
    )


def find_item(ctx: Context, user_id: int, key: str) -> dict | None:
    """One show's card for the detail panel, scored for this viewer."""
    profile = ctx.profile(user_id)
    if key.startswith("show:"):
        show_id = int(key[5:])
        if show_id not in ctx.lib.shows:
            return None
        w = profile.watches.get(show_id)
        if w is not None and w.state in ("watching", "paused"):
            item = progress_item(
                ctx.session,
                ctx.lib,
                w,
                profile.user,
                ctx.settings,
                profile.prefs,
                ctx.machine,
                ctx.now,
            )
            if item:
                return item
        scored = score_library(ctx.lib, profile, ctx.others(user_id), ctx.now, include={show_id})
        if show_id in scored:
            rank = _percentiles(list(scored.values()))
            return candidate_item(ctx.lib, scored[show_id], profile.prefs, ctx.machine, rank)
        return show_item(ctx.lib, show_id, profile.prefs, ctx.machine)
    return None


def pitch_material(
    ctx: Context, user_id: int, item: dict
) -> tuple[str, str, str, list[str], list[str]]:
    """What the model gets for a pitch: show-level facts and this viewer's favourites."""
    profile = ctx.profile(user_id)
    liked = sorted(
        (w for w in profile.watches.values() if w.affinity >= 0.7 and w.show_id in ctx.lib.shows),
        key=lambda w: w.affinity * w.weight,
        reverse=True,
    )
    facts = item.get("facts") or {}
    details = ", ".join(
        str(x)
        for x in (
            ", ".join(facts.get("genres") or []),
            f"{facts['seasons']} seasons" if facts.get("seasons") else None,
            facts.get("status"),
        )
        if x
    )
    return (
        item["title"],
        item.get("overview") or item.get("tagline") or "",
        details,
        [ctx.lib.shows[w.show_id].title for w in liked[:6]],
        [r["text"] for r in item.get("reasons") or []],
    )
