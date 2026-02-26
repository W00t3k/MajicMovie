"""Background movie enrichment service.

Enriches all movies with:
- Poster URL (from iTunes/TMDB)
- Overview/description
- Release date
- Availability status (assume available if > 6 months old)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.clients.poster_lookup_client import PosterLookupClient
    from app.services.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class EnrichmentService:
    """Background service to enrich movie data."""

    def __init__(
        self,
        memory_store: MemoryStore,
        poster_client: PosterLookupClient,
        batch_size: int = 20,
        delay_between_batches: float = 2.0,
    ):
        self._memory_store = memory_store
        self._poster_client = poster_client
        self._batch_size = batch_size
        self._delay = delay_between_batches
        self._running = False
        self._task: asyncio.Task | None = None
        self._enriched_count = 0
        self._error_count = 0

    async def enrich_movie(self, title: str, year: int | None) -> bool:
        """Enrich a single movie with poster, overview, and availability."""
        try:
            # Check if already enriched
            cached = self._memory_store.get_movie_cache(title, year)
            if cached and cached.get("poster_url") and cached.get("overview"):
                return True  # Already complete

            # Fetch from poster lookup (iTunes/TMDB)
            info = await self._poster_client.lookup(title, year)

            poster_url = info.get("poster_url") if info else None
            overview = info.get("overview") if info else None
            genres = info.get("genres") if info else None
            release_date = info.get("release_date") if info else None

            # Determine availability: assume available if > 6 months old
            available = False
            if year:
                release_year_date = datetime(year, 7, 1)  # Mid-year estimate
                if datetime.now() - release_year_date > timedelta(days=180):
                    available = True

            # If we have a specific release date, use that
            if release_date:
                try:
                    rd = datetime.fromisoformat(release_date.replace("Z", "+00:00"))
                    if datetime.now(rd.tzinfo) - rd > timedelta(days=180):
                        available = True
                except (ValueError, TypeError):
                    pass

            # Cache the enriched data
            self._memory_store.set_movie_cache(
                title=title,
                year=year,
                poster_url=poster_url,
                overview=overview,
                genres=genres,
                release_date=release_date,
                available_usenet=available,
            )

            self._enriched_count += 1
            return True

        except Exception as e:
            logger.warning(f"Failed to enrich {title} ({year}): {e}")
            self._error_count += 1
            return False

    async def enrich_batch(self, movies: list[dict]) -> int:
        """Enrich a batch of movies concurrently."""
        tasks = [
            self.enrich_movie(m["title"], m.get("year"))
            for m in movies
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return sum(1 for r in results if r is True)

    async def enrich_all_recommendations(self, recommendations: list) -> None:
        """Queue all recommendations for enrichment."""
        for rec in recommendations:
            movie = rec.get("movie") or rec
            if not movie:
                continue
            title = movie.get("title")
            year = movie.get("year")
            if title:
                # Add to cache if not exists (will be enriched in background)
                cached = self._memory_store.get_movie_cache(title, year)
                if not cached:
                    self._memory_store.set_movie_cache(title=title, year=year)

    async def run_background_enrichment(self) -> None:
        """Continuously enrich movies in the background."""
        self._running = True
        logger.info("Starting background enrichment service")

        while self._running:
            try:
                # Get unenriched movies
                unenriched = self._memory_store.get_unenriched_movies(limit=self._batch_size)

                if not unenriched:
                    # All caught up, wait before checking again
                    await asyncio.sleep(60)
                    continue

                # Enrich batch
                count = await self.enrich_batch(unenriched)
                logger.debug(f"Enriched {count}/{len(unenriched)} movies")

                # Delay between batches to avoid rate limiting
                await asyncio.sleep(self._delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Enrichment error: {e}")
                await asyncio.sleep(10)

        logger.info("Background enrichment service stopped")

    def start(self) -> None:
        """Start the background enrichment task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_background_enrichment())
            logger.info("Background enrichment task started")

    def stop(self) -> None:
        """Stop the background enrichment task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Background enrichment task stopped")

    def stats(self) -> dict:
        """Get enrichment statistics."""
        cache_stats = self._memory_store.movie_cache_stats()
        return {
            "running": self._running,
            "enriched_count": self._enriched_count,
            "error_count": self._error_count,
            **cache_stats,
        }
