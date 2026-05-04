"""Trakt.tv API client.

Configure in .env:
  TRAKT_CLIENT_ID=...       # from https://trakt.tv/oauth/applications
  TRAKT_ACCESS_TOKEN=...    # OAuth access token (see README for setup)
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

TRAKT_API_BASE = "https://api.trakt.tv"


class TraktClient:
    def __init__(self, client_id: str, access_token: str | None = None, timeout_seconds: float = 10.0):
        self._client_id = client_id
        self._access_token = access_token
        self._timeout = httpx.Timeout(timeout_seconds)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _get(self, path: str, params: dict | None = None) -> list | dict | None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{TRAKT_API_BASE}{path}", headers=self._headers(), params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.warning("Trakt API error %s: %s", path, exc)
            return None

    async def watchlist_movies(self) -> list[dict]:
        """Get the authenticated user's movie watchlist."""
        data = await self._get("/sync/watchlist/movies/added")
        if not data or not isinstance(data, list):
            return []
        return [
            {
                "title": item["movie"]["title"],
                "year": item["movie"].get("year"),
                "imdb_id": item["movie"].get("ids", {}).get("imdb"),
                "trakt_id": item["movie"].get("ids", {}).get("trakt"),
                "slug": item["movie"].get("ids", {}).get("slug"),
            }
            for item in data
            if isinstance(item, dict) and "movie" in item
        ]

    async def watched_movies(self) -> list[dict]:
        """Get the authenticated user's watched movie history."""
        data = await self._get("/sync/watched/movies")
        if not data or not isinstance(data, list):
            return []
        return [
            {
                "title": item["movie"]["title"],
                "year": item["movie"].get("year"),
                "plays": item.get("plays", 1),
                "last_watched": item.get("last_watched_at"),
                "imdb_id": item["movie"].get("ids", {}).get("imdb"),
            }
            for item in data
            if isinstance(item, dict) and "movie" in item
        ]

    async def rated_movies(self) -> list[dict]:
        """Get the authenticated user's movie ratings."""
        data = await self._get("/sync/ratings/movies")
        if not data or not isinstance(data, list):
            return []
        return [
            {
                "title": item["movie"]["title"],
                "year": item["movie"].get("year"),
                "rating": item.get("rating"),
                "rated_at": item.get("rated_at"),
                "imdb_id": item["movie"].get("ids", {}).get("imdb"),
            }
            for item in data
            if isinstance(item, dict) and "movie" in item
        ]

    async def trending_movies(self, limit: int = 50) -> list[dict]:
        """Get trending movies (no auth required)."""
        data = await self._get("/movies/trending", params={"limit": limit, "extended": "full"})
        if not data or not isinstance(data, list):
            return []
        return [
            {
                "title": item["movie"]["title"],
                "year": item["movie"].get("year"),
                "watchers": item.get("watchers", 0),
                "imdb_id": item["movie"].get("ids", {}).get("imdb"),
                "overview": item["movie"].get("overview"),
                "rating": item["movie"].get("rating"),
            }
            for item in data
            if isinstance(item, dict) and "movie" in item
        ]

    async def popular_movies(self, limit: int = 50) -> list[dict]:
        """Get popular movies (no auth required)."""
        data = await self._get("/movies/popular", params={"limit": limit, "extended": "full"})
        if not data or not isinstance(data, list):
            return []
        return [
            {
                "title": item["title"],
                "year": item.get("year"),
                "imdb_id": item.get("ids", {}).get("imdb"),
                "overview": item.get("overview"),
                "rating": item.get("rating"),
            }
            for item in data
            if isinstance(item, dict)
        ]

    async def test_connection(self) -> tuple[bool, str]:
        """Test the Trakt API connection."""
        try:
            data = await self._get("/movies/trending", params={"limit": 1})
            if data:
                return True, "Trakt.tv connected successfully"
            return False, "Empty response from Trakt"
        except Exception as exc:
            return False, str(exc)
