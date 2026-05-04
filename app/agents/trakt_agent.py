"""Trakt.tv agent — syncs watchlist, watched history, trending and popular movies."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.agents.base import BaseAgent, AgentResult
from app.models import AgentContext, MovieCandidate

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TraktAgent(BaseAgent):
    name = "trakt"

    def __init__(
        self,
        client_id: str | None = None,
        access_token: str | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._client_id = client_id
        self._access_token = access_token
        self._timeout = timeout_seconds

    async def collect(self, context: AgentContext) -> AgentResult:
        if not self._client_id:
            return AgentResult(movies=[], metadata={"notes": "Trakt not configured (missing TRAKT_CLIENT_ID)"}, skipped=True)

        from app.clients.trakt_client import TraktClient
        client = TraktClient(
            client_id=self._client_id,
            access_token=self._access_token,
            timeout_seconds=self._timeout,
        )

        movies: list[MovieCandidate] = []
        seen_titles: set[str] = set()

        def _add(items: list[dict], tag: str, boost: float = 0.0) -> None:
            for item in items:
                title = item.get("title", "").strip()
                year = item.get("year")
                if not title:
                    continue
                key = f"{title.lower()}::{year}"
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                movies.append(MovieCandidate(
                    title=title,
                    year=year,
                    source_tags=[f"trakt-{tag}"],
                    overview=item.get("overview"),
                    score_boost=boost,
                ))

        try:
            # Personal data (higher boost — user explicitly wants these)
            if self._access_token:
                watchlist = await client.watchlist_movies()
                _add(watchlist, "watchlist", boost=0.3)

                watched = await client.watched_movies()
                _add(watched, "watched", boost=0.0)

                rated = await client.rated_movies()
                # Boost highly-rated movies
                for item in rated:
                    title = item.get("title", "").strip()
                    year = item.get("year")
                    rating = item.get("rating", 0) or 0
                    if not title:
                        continue
                    key = f"{title.lower()}::{year}"
                    if key in seen_titles:
                        continue
                    seen_titles.add(key)
                    boost = (rating - 5) * 0.05 if rating > 5 else 0.0
                    movies.append(MovieCandidate(
                        title=title,
                        year=year,
                        source_tags=["trakt-rated"],
                        score_boost=boost,
                    ))

            # Public trending/popular (no auth needed)
            trending = await client.trending_movies(limit=50)
            _add(trending, "trending", boost=0.1)

            popular = await client.popular_movies(limit=50)
            _add(popular, "popular", boost=0.05)

        except Exception as exc:
            logger.warning("TraktAgent error: %s", exc)
            return AgentResult(movies=[], metadata={"error": str(exc)}, skipped=True)

        return AgentResult(
            movies=movies,
            metadata={"notes": f"Loaded {len(movies)} movies from Trakt.tv"},
        )
