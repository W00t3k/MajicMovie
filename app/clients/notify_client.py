"""Push notification client supporting ntfy.sh, Pushover, and generic webhooks.

Configure in .env:
  NTFY_TOPIC=majic-movies          # ntfy.sh topic (free, no account needed)
  NTFY_BASE_URL=https://ntfy.sh    # default
  PUSHOVER_TOKEN=...               # optional Pushover app token
  PUSHOVER_USER=...                # optional Pushover user key
  DIGEST_WEBHOOK_URL=...           # optional generic POST webhook
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class NotifyClient:
    """Send push notifications via ntfy.sh (primary) with optional Pushover/webhook fallback."""

    def __init__(
        self,
        ntfy_topic: str | None = None,
        ntfy_base_url: str = "https://ntfy.sh",
        pushover_token: str | None = None,
        pushover_user: str | None = None,
        webhook_url: str | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._ntfy_topic = ntfy_topic
        self._ntfy_base_url = ntfy_base_url.rstrip("/")
        self._pushover_token = pushover_token
        self._pushover_user = pushover_user
        self._webhook_url = webhook_url
        self._timeout = httpx.Timeout(timeout_seconds)

    @property
    def available(self) -> bool:
        return bool(self._ntfy_topic or (self._pushover_token and self._pushover_user) or self._webhook_url)

    async def send(
        self,
        title: str,
        message: str,
        url: str | None = None,
        tags: list[str] | None = None,
        priority: str = "default",
    ) -> bool:
        """Send a notification. Returns True if at least one channel succeeded."""
        success = False

        if self._ntfy_topic:
            success = await self._send_ntfy(title, message, url=url, tags=tags or [], priority=priority) or success

        if self._pushover_token and self._pushover_user:
            success = await self._send_pushover(title, message, url=url) or success

        if self._webhook_url:
            success = await self._send_webhook(title, message, url=url) or success

        return success

    async def notify_movie_available(self, movie_title: str, year: int | None, source: str, nzb_url: str | None = None) -> bool:
        """Send a notification when a wanted movie appears on Usenet."""
        year_str = f" ({year})" if year else ""
        title = f"🎬 {movie_title}{year_str} is available!"
        message = f"Found on {source}. Ready to download."
        tags = ["movie_camera", "tada"]
        return await self.send(title=title, message=message, url=nzb_url, tags=tags, priority="high")

    async def notify_digest_ready(self, top_picks: list[str]) -> bool:
        """Send a notification when the weekly digest is ready."""
        title = "🎬 Your Weekly Movie Picks Are Ready!"
        picks_text = "\n".join(f"• {p}" for p in top_picks[:5])
        message = f"Top picks this week:\n{picks_text}"
        tags = ["popcorn", "calendar"]
        return await self.send(title=title, message=message, tags=tags)

    async def _send_ntfy(
        self,
        title: str,
        message: str,
        url: str | None = None,
        tags: list[str] | None = None,
        priority: str = "default",
    ) -> bool:
        try:
            headers: dict[str, str] = {
                "Title": title,
                "Priority": priority,
                "Content-Type": "text/plain",
            }
            if tags:
                headers["Tags"] = ",".join(tags)
            if url:
                headers["Click"] = url

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._ntfy_base_url}/{self._ntfy_topic}",
                    content=message.encode(),
                    headers=headers,
                )
                resp.raise_for_status()
                logger.info("ntfy notification sent: %s", title)
                return True
        except Exception as exc:
            logger.warning("ntfy notification failed: %s", exc)
            return False

    async def _send_pushover(self, title: str, message: str, url: str | None = None) -> bool:
        try:
            payload: dict = {
                "token": self._pushover_token,
                "user": self._pushover_user,
                "title": title,
                "message": message,
            }
            if url:
                payload["url"] = url

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post("https://api.pushover.net/1/messages.json", data=payload)
                resp.raise_for_status()
                logger.info("Pushover notification sent: %s", title)
                return True
        except Exception as exc:
            logger.warning("Pushover notification failed: %s", exc)
            return False

    async def _send_webhook(self, title: str, message: str, url: str | None = None) -> bool:
        try:
            payload = {"title": title, "message": message, "url": url}
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._webhook_url, json=payload)  # type: ignore[arg-type]
                resp.raise_for_status()
                logger.info("Webhook notification sent: %s", title)
                return True
        except Exception as exc:
            logger.warning("Webhook notification failed: %s", exc)
            return False
