"""Configuration.

Values come from environment variables (or a `.env` file). Anything in
`RUNTIME_KEYS` can also be changed from the Settings page; those overrides are
stored in the `settings` table and take precedence over the environment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    config_dir: Path = Path("/config")
    database_url: str = ""

    # Web
    host: str = "0.0.0.0"
    port: int = 8765
    public_url: str = "http://localhost:8765"
    webhook_secret: str = ""

    # Media servers
    plex_url: str = ""
    plex_token: str = ""
    plex_libraries: str = ""  # comma-separated library names; empty means all TV libraries
    tautulli_url: str = ""
    tautulli_api_key: str = ""
    verify_ssl: bool = True  # Plex over https by IP needs this off (cert is for *.plex.direct)
    media_path_map: str = ""  # "<plex path>=<path in this container>", separated by ';'

    # LLM (any OpenAI-compatible endpoint; Ollama is detected and uses its native API)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen2.5:14b"
    llm_api: Literal["auto", "openai", "ollama"] = "auto"
    llm_context_tokens: int = 16384
    llm_temperature: float = 0.2
    llm_timeout_seconds: float = 900
    verify_model: str = ""  # model for the verification pass; defaults to llm_model

    # Sources
    subtitle_languages: str = "en,eng"
    whisper_enabled: bool = True
    whisper_model: str = "large-v3"
    whisper_device: str = "cuda"
    whisper_compute_type: str = ""  # default: float16 on cuda, int8 on cpu
    tmdb_api_key: str = ""
    # "dialogue": summarize each episode's subtitles with the model (TMDB/Plex synopsis and
    # TMDB cast as grounding). "synopsis": build summaries from the synopsis and cast alone,
    # with no model call; much faster, less detailed.
    summary_source: Literal["dialogue", "synopsis"] = "dialogue"
    allow_overview_only: bool = True  # summarize from the synopsis when there is no dialogue

    # Sonarr (optional): Up Next can add a recommended show that isn't in Plex yet
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    sonarr_quality_profile: str = ""  # name; blank uses the first
    sonarr_root_folder: str = ""  # path; blank uses the first
    sonarr_search: bool = True  # search for episodes as soon as it's added

    # Notifications
    ntfy_url: str = ""
    ntfy_token: str = ""
    discord_webhook_url: str = ""

    # Behaviour
    watched_percent: int = 90
    default_gap_days: int = 10
    recap_hide_title: bool = True
    recap_verify: bool = True
    recap_detail_episodes: int = 3
    recap_cache_hours: int = 72

    # Scheduling
    sync_interval_minutes: int = 15
    library_scan_interval_minutes: int = 360
    # Login. Setting an admin password turns it on; Plex sign-in then works for everyone
    # with access to the Plex server (the server owner is an admin).
    admin_password: str = ""
    plex_login: bool = True

    # Off: the GPU only runs when someone asks (Index, Recap). On: index a show when anyone
    # starts watching it, and catch up overnight at backfill_hour.
    auto_index: bool = False
    backfill_hour: int = 3  # local hour for the nightly backfill; -1 disables it
    worker_poll_seconds: float = 1.0

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.config_dir / 'wherewerewe.db'}"

    @property
    def effective_verify_model(self) -> str:
        return self.verify_model or self.llm_model

    @property
    def languages(self) -> list[str]:
        return [x.strip().lower() for x in self.subtitle_languages.split(",") if x.strip()]

    @property
    def sources_dir(self) -> Path:
        return self.config_dir / "sources"

    @property
    def cache_dir(self) -> Path:
        return self.config_dir / "cache"


# Keys the Settings page may change at runtime, grouped for display.
RUNTIME_KEYS: dict[str, list[str]] = {
    "Connections": [
        "plex_url",
        "plex_token",
        "plex_libraries",
        "tautulli_url",
        "tautulli_api_key",
        "verify_ssl",
        "media_path_map",
        "tmdb_api_key",
        "public_url",
        "webhook_secret",
    ],
    "Model": [
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_api",
        "llm_context_tokens",
        "llm_temperature",
        "verify_model",
    ],
    "Sources": [
        "summary_source",
        "subtitle_languages",
        "whisper_enabled",
        "whisper_model",
        "whisper_device",
        "allow_overview_only",
    ],
    "Recaps": [
        "watched_percent",
        "default_gap_days",
        "recap_hide_title",
        "recap_verify",
        "recap_detail_episodes",
        "recap_cache_hours",
    ],
    "Notifications": ["ntfy_url", "ntfy_token", "discord_webhook_url"],
    "Sonarr": [
        "sonarr_url",
        "sonarr_api_key",
        "sonarr_quality_profile",
        "sonarr_root_folder",
        "sonarr_search",
    ],
    "Security": ["admin_password", "plex_login"],
    "Schedule": [
        "sync_interval_minutes",
        "library_scan_interval_minutes",
        "auto_index",
        "backfill_hour",
    ],
}
SECRET_KEYS = {
    "admin_password",
    "plex_token",
    "tautulli_api_key",
    "llm_api_key",
    "tmdb_api_key",
    "sonarr_api_key",
    "ntfy_token",
    "webhook_secret",
    "discord_webhook_url",
}
ALL_RUNTIME_KEYS = {k for keys in RUNTIME_KEYS.values() for k in keys}


def parse_setting(key: str, raw: Any) -> Any:
    """Validate a raw value (usually a string from the DB or a form) for a settings key."""
    if key not in ALL_RUNTIME_KEYS:
        raise KeyError(key)
    annotation = Settings.model_fields[key].annotation
    if isinstance(raw, str) and annotation is bool:
        raw = raw.strip().lower() in {"1", "true", "yes", "on"}
    return TypeAdapter(annotation).validate_python(raw, strict=False)


def base_settings() -> Settings:
    return Settings()


def apply_overrides(base: Settings, overrides: dict[str, str]) -> Settings:
    update: dict[str, Any] = {}
    for key, raw in overrides.items():
        if key in ALL_RUNTIME_KEYS:
            try:
                update[key] = parse_setting(key, raw)
            except Exception:  # a bad stored value must not take the app down
                continue
    return base.model_copy(update=update)
