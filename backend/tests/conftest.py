from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest

from wherewerewe import db
from wherewerewe.llm import client as llm_client
from wherewerewe.llm import prompts


class FakeLLM:
    """Deterministic stand-in for a model. Records every call."""

    def __init__(self, recap_text: str = "", unsupported: tuple[str, ...] = ()):
        self.model = "fake-model"
        self.context_tokens = 16384
        self.calls: list[dict] = []
        self.recap_text = recap_text or (
            "Walt is a chemistry teacher. Jesse is his partner. Things are tense."
        )
        self.unsupported = unsupported

    def _kind(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        for name, text in (
            ("summary", prompts.SUMMARY_SYSTEM),
            ("chunk", prompts.CHUNK_SYSTEM),
            ("rollup", prompts.ROLLUP_SYSTEM),
            ("verify", prompts.VERIFY_SYSTEM),
            ("whois", prompts.WHOIS_SYSTEM),
            ("recap", prompts.RECAP_SYSTEM),
        ):
            if system == text:
                return name
        return "other"

    def chat(self, messages, *, schema=None, max_tokens=None) -> str:
        kind = self._kind(messages)
        user = messages[-1]["content"]
        self.calls.append({"kind": kind, "messages": messages, "schema": schema})
        if kind == "summary":
            code = re.search(r"Episode: (\S+)", messages[1]["content"]).group(1)
            lines = [
                ln
                for ln in messages[1]["content"].splitlines()
                if ln and not ln.startswith(("Episode:", "Dialogue", "Official", "Write"))
            ]
            speakers = sorted(
                {m.group(1) for ln in lines if (m := re.match(r"^([A-Z][a-z]+): ", ln))}
            )
            return json.dumps(
                {
                    "episode": code,
                    "plot_beats": [f"{code} beat: {ln}" for ln in lines[:3]],
                    "characters": [
                        {
                            "name": s,
                            "aliases": [],
                            "role": f"{s} role",
                            "status": "injured" if code == "S01E02" else "alive",
                            "new_info": f"{s} news in {code}",
                            "relationships": [],
                        }
                        for s in speakers
                    ],
                    "relationships_changed": [],
                    "open_threads": [f"thread from {code}"],
                    "resolved_threads": [],
                    "cliffhanger": f"cliffhanger of {code}",
                }
            )
        if kind == "chunk":
            return "notes for this part"
        if kind == "rollup":
            season = re.search(r"season (\d+)", user).group(1)
            return f"Season {season} rollup: everything that happened."
        if kind == "verify":
            results = []
            for sid, sentence in re.findall(r"^\[(\d+)\] (.*)$", user, re.MULTILINE):
                bad = any(marker in sentence for marker in self.unsupported)
                results.append(
                    {"id": int(sid), "supported": not bad, "reason": "not in source" if bad else ""}
                )
            return json.dumps({"results": results})
        if kind == "whois":
            return "They are someone you have met."
        return "ok"

    def stream(self, messages, *, max_tokens=None) -> Iterator[str]:
        self.calls.append({"kind": self._kind(messages), "messages": messages, "schema": None})
        for i in range(0, len(self.recap_text), 7):
            yield self.recap_text[i : i + 7]

    def list_models(self) -> list[str]:
        return [self.model]

    def ping(self) -> str:
        return "ok (fake)"


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm_client, "llm_factory", lambda settings, verify=False: fake)
    return fake


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own config dir and SQLite database."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    for var in (
        "PLEX_URL",
        "PLEX_TOKEN",
        "TAUTULLI_URL",
        "TAUTULLI_API_KEY",
        "TMDB_API_KEY",
        "NTFY_URL",
        "DISCORD_WEBHOOK_URL",
        "DATABASE_URL",
        "LLM_MODEL",
        "WEBHOOK_SECRET",
        "MEDIA_PATH_MAP",
        "SUMMARY_SOURCE",
        "AUTO_INDEX",
        "ADMIN_PASSWORD",
        "SONARR_URL",
        "SONARR_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WHISPER_ENABLED", "false")
    (tmp_path / "config").mkdir()
    db.init_engine(f"sqlite:///{tmp_path / 'config' / 'test.db'}")
    db.create_all()
    yield
    db.get_engine().dispose()


def write_srt(path, lines: list[str]) -> None:
    blocks = []

    def ts(seconds: int) -> str:
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d},000"

    for i, line in enumerate(lines, 1):
        blocks.append(f"{i}\n{ts(i * 3)} --> {ts(i * 3 + 2)}\n{line}\n")
    path.write_text("\n".join(blocks))
