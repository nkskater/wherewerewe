"""Push notifications through ntfy or a Discord webhook."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from ..config import Settings

NTFY_LIMIT = 3900
DISCORD_LIMIT = 4000


def is_discord(target: str) -> bool:
    host = urlsplit(target).netloc.lower()
    return host.endswith(("discord.com", "discordapp.com")) and "/api/webhooks/" in target


def send(
    target: str,
    title: str,
    message: str,
    click_url: str | None,
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Send to a ntfy topic URL or a Discord webhook URL. Returns the channel used."""
    with httpx.Client(timeout=20, transport=transport) as client:
        if is_discord(target):
            embed = {"title": title[:250], "description": message[:DISCORD_LIMIT]}
            if click_url:
                embed["url"] = click_url
            resp = client.post(target, json={"embeds": [embed]})
            channel = "discord"
        else:
            parts = urlsplit(target)
            path = parts.path.rstrip("/")
            topic = path.rsplit("/", 1)[-1]
            if not parts.scheme or not topic:
                raise ValueError(f"not a ntfy topic URL: {target!r}")
            server_path = path[: -len(topic)].rstrip("/")
            body: dict = {
                "topic": topic,
                "title": title,
                "message": message[:NTFY_LIMIT],
                "tags": ["tv"],
            }
            if click_url:
                body["click"] = click_url
            headers = (
                {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
            )
            resp = client.post(
                f"{parts.scheme}://{parts.netloc}{server_path}/", json=body, headers=headers
            )
            channel = "ntfy"
    if resp.status_code >= 300:
        raise RuntimeError(f"{channel} returned {resp.status_code}: {resp.text[:200]}")
    return channel


def targets_for(user_target: str | None, settings: Settings) -> list[str]:
    if user_target:
        return [user_target]
    return [t for t in (settings.ntfy_url, settings.discord_webhook_url) if t]
