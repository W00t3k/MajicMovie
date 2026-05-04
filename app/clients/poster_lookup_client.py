from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from app.clients.http_client import HTTPClient
from app.clients.tmdb_client import TMDBClient

if TYPE_CHECKING:
    from app.services.memory_store import MemoryStore

logger = logging.getLogger(__name__)

# Persist cache to DB every N lookups
PERSIST_EVERY_N_LOOKUPS = 50

# Sentinel value for confirmed-missing posters (cache failures to stop retrying)
_MISSING_SENTINEL = "__missing__"


class PosterLookupClient:
    ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
    TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

    # TMDB genre ID to name mapping
    TMDB_GENRES = {
        28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
        99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
        27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
        878: "Sci-Fi", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    }

    def __init__(
        self,
        timeout_seconds: float,
        tmdb_api_key: str | None = None,
        fanart_api_key: str | None = None,
        memory_store: "MemoryStore | None" = None,
    ):
        self._http = HTTPClient(timeout_seconds)
        self._cache: dict[str, Any] = {}
        self._memory_store = memory_store
        self._lookup_count = 0
        self._fanart_api_key = fanart_api_key
        self._tmdb_client = (
            TMDBClient(api_key=tmdb_api_key, timeout_seconds=timeout_seconds)
            if tmdb_api_key
            else None
        )
        # Phase 3: Load cache from persistent storage on startup
        self._load_cache_from_db()

    def _load_cache_from_db(self) -> None:
        """Load poster cache from SQLite on startup."""
        if not self._memory_store:
            return
        try:
            cached_data, synced_at = self._memory_store.get_catalog_cache("posters")
            if cached_data and isinstance(cached_data, list):
                # Convert list of [key, value] pairs back to dict
                self._cache = {item[0]: item[1] for item in cached_data if len(item) == 2}
                logger.info("Loaded %d poster cache entries from DB (synced: %s)", len(self._cache), synced_at)
        except Exception as e:
            logger.warning("Failed to load poster cache from DB: %s", e)

    def _persist_cache_to_db(self) -> None:
        """Persist poster cache to SQLite."""
        if not self._memory_store:
            return
        try:
            # Convert dict to list of [key, value] pairs for JSON serialization
            cache_list = [[k, v] for k, v in self._cache.items()]
            self._memory_store.set_catalog_cache("posters", cache_list)
            logger.debug("Persisted %d poster cache entries to DB", len(self._cache))
        except Exception as e:
            logger.warning("Failed to persist poster cache to DB: %s", e)

    def get_cache_stats(self) -> dict[str, Any]:
        """Get poster cache statistics."""
        return {
            "entries": len(self._cache),
            "lookups_since_persist": self._lookup_count % PERSIST_EVERY_N_LOOKUPS,
            "persist_threshold": PERSIST_EVERY_N_LOOKUPS,
            "has_memory_store": self._memory_store is not None,
        }

    async def poster_for(self, title: str, year: int | None = None) -> str | None:
        info = await self.lookup(title, year)
        return info.get("poster_url") if info else None

    async def lookup(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        query = " ".join(part for part in [title.strip(), str(year) if year else ""] if part)
        cache_key = query.lower()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if cached == _MISSING_SENTINEL:
                return None  # Confirmed missing, stop retrying
            if isinstance(cached, dict):
                return cached
            return {"poster_url": cached}

        result = await self._lookup_itunes(title, year)

        # Try TMDB
        if not result or not result.get("poster_url"):
            tmdb_result = await self._lookup_tmdb(title, year)
            if tmdb_result:
                result = self._merge_result(result, tmdb_result)

        # Try FanArt.tv (excellent movie poster database)
        if not result or not result.get("poster_url"):
            fanart_result = await self._lookup_fanart(title, year)
            if fanart_result:
                result = self._merge_result(result, fanart_result)

        # Try OMDB
        if not result or not result.get("poster_url"):
            omdb_result = await self._lookup_omdb(title, year)
            if omdb_result:
                result = self._merge_result(result, omdb_result)

        # Try Letterboxd
        if not result or not result.get("poster_url"):
            letterboxd_result = await self._lookup_letterboxd(title, year)
            if letterboxd_result:
                result = self._merge_result(result, letterboxd_result)

        # Try IMDB
        if not result or not result.get("poster_url"):
            imdb_result = await self._lookup_imdb(title, year)
            if imdb_result:
                result = self._merge_result(result, imdb_result)

        # Try Rotten Tomatoes
        if not result or not result.get("poster_url"):
            rt_result = await self._lookup_rottentomatoes(title, year)
            if rt_result:
                result = self._merge_result(result, rt_result)

        # Try Wikipedia as last resort
        if not result or not result.get("poster_url"):
            wiki_result = await self._lookup_wikipedia(title, year)
            if wiki_result:
                result = self._merge_result(result, wiki_result)

        if result:
            self._cache[cache_key] = result
            self._lookup_count += 1
            if self._lookup_count % PERSIST_EVERY_N_LOOKUPS == 0:
                self._persist_cache_to_db()
        else:
            # Cache missing sentinel so we stop retrying on every request
            self._cache[cache_key] = _MISSING_SENTINEL

        return result or None

    async def _lookup_itunes(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        query = " ".join(part for part in [title.strip(), str(year) if year else ""] if part)
        try:
            payload = await self._http.get_json(
                self.ITUNES_SEARCH_URL,
                params={"term": query, "entity": "movie", "media": "movie", "limit": 10},
            )
        except Exception:  # noqa: BLE001
            return None

        results = payload.get("results", []) if isinstance(payload, dict) else []
        for item in results:
            if not isinstance(item, dict):
                continue
            track_name = str(item.get("trackName") or "").strip()
            release_date = str(item.get("releaseDate") or "")
            item_year = self._extract_year(release_date)
            if not track_name:
                continue
            if not self._title_like_match(title, track_name):
                continue
            if year is not None and item_year is not None and abs(year - item_year) > 1:
                continue
            artwork = item.get("artworkUrl100") or item.get("artworkUrl60")
            poster_url = self._upgrade_artwork_url(artwork) if isinstance(artwork, str) and artwork else None
            overview = str(item.get("longDescription") or "").strip() or None
            genre = str(item.get("primaryGenreName") or "").strip() or None
            release_date_iso = self._coerce_iso_release_date(release_date)
            result: dict[str, Any] = {}
            if poster_url:
                result["poster_url"] = poster_url
            if overview:
                result["overview"] = overview
            if genre:
                result["genre"] = genre
            if release_date_iso:
                result["release_date"] = release_date_iso
            return result or None
        return None

    async def _lookup_tmdb(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        if not self._tmdb_client:
            return None

        result = await self._tmdb_search_best(title, year)
        if result:
            return result

        # Retry without year for unreleased / upcoming movies
        if year is not None:
            result = await self._tmdb_search_best(title, None)
            if result:
                return result

        # Try with simplified title (remove common suffixes like Pilot, Part X, etc.)
        simplified = self._simplify_title(title)
        if simplified and simplified.lower() != title.lower():
            result = await self._tmdb_search_best(simplified, year)
            if result:
                return result
            if year is not None:
                result = await self._tmdb_search_best(simplified, None)
                if result:
                    return result

        return None

    @staticmethod
    def _simplify_title(title: str) -> str:
        """Remove common suffixes that might prevent TMDB matching."""
        import re
        # Remove common suffixes
        patterns = [
            r'\s+Pilot$',
            r'\s+Part\s*\d+$',
            r'\s+Episode\s*\d+$',
            r'\s+Chapter\s*\d+$',
            r'\s+Vol\.?\s*\d+$',
            r'\s+Volume\s*\d+$',
            r'\s+Season\s*\d+$',
            r'\s+S\d+E\d+$',
            r'\s+\d{4}$',  # Year at end
        ]
        result = title.strip()
        for pattern in patterns:
            result = re.sub(pattern, '', result, flags=re.IGNORECASE)
        return result.strip()

    async def _tmdb_search_best(self, title: str, year: int | None) -> dict[str, Any] | None:
        try:
            results = await self._tmdb_client.search_movie(query=title.strip(), year=year)
        except Exception:  # noqa: BLE001
            return None

        first_with_poster: dict[str, Any] | None = None

        for item in results:
            if not isinstance(item, dict):
                continue
            item_title = str(item.get("title") or "").strip()
            if not item_title:
                continue
            poster_path = item.get("poster_path")
            poster_url = f"{self.TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
            overview = str(item.get("overview") or "").strip() or None
            release_date_iso = self._coerce_iso_release_date(item.get("release_date"))

            # Extract genres from TMDB genre_ids
            genre_ids = item.get("genre_ids", [])
            genres = [self.TMDB_GENRES[gid] for gid in genre_ids if gid in self.TMDB_GENRES]

            # Strict title match — return immediately
            if self._title_like_match(title, item_title):
                result: dict[str, Any] = {}
                if poster_url:
                    result["poster_url"] = poster_url
                if overview:
                    result["overview"] = overview
                if genres:
                    result["genres"] = genres
                    result["genre"] = genres[0] if genres else None
                if release_date_iso:
                    result["release_date"] = release_date_iso
                return result or None

            # Track first result that has a poster as fallback
            if poster_url and first_with_poster is None:
                first_with_poster = {"poster_url": poster_url}
                if overview:
                    first_with_poster["overview"] = overview
                if genres:
                    first_with_poster["genres"] = genres
                    first_with_poster["genre"] = genres[0] if genres else None
                if release_date_iso:
                    first_with_poster["release_date"] = release_date_iso

        # No strict match — accept the top TMDB result (search is relevance-ranked)
        return first_with_poster

    @staticmethod
    def _extract_year(text: str) -> int | None:
        match = re.search(r"(19|20)\d{2}", text)
        return int(match.group(0)) if match else None

    @staticmethod
    def _coerce_iso_release_date(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        return match.group(1) if match else None

    @staticmethod
    def _title_like_match(a: str, b: str) -> bool:
        normalize = lambda value: re.sub(r"[^a-z0-9]+", "", value.lower())
        strip_articles = lambda value: re.sub(r"\b(the|a|an)\b", "", value.lower())
        left = normalize(a)
        right = normalize(b)
        if not left or not right:
            return False
        if left == right:
            return True
        if left in right or right in left:
            return True
        left2 = normalize(strip_articles(a))
        right2 = normalize(strip_articles(b))
        if left2 and right2 and (left2 in right2 or right2 in left2):
            return True
        # Fuzzy: if the shorter string is at least 6 chars and matches 85%+ of the longer
        shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
        if len(shorter) >= 6:
            matches = sum(1 for i, c in enumerate(shorter) if i < len(longer) and c == longer[i])
            if matches / len(shorter) >= 0.85:
                return True
        return False

    @staticmethod
    def _upgrade_artwork_url(url: str) -> str:
        return url.replace("100x100bb", "600x600bb").replace("60x60bb", "600x600bb")

    async def _lookup_fanart(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        """Try FanArt.tv for high-quality movie posters."""
        if not self._fanart_api_key and not self._tmdb_client:
            return None
        try:
            # First get TMDB ID via search
            if not self._tmdb_client:
                return None
            results = await self._tmdb_client.search_movie(query=title.strip(), year=year)
            tmdb_id = None
            for item in results:
                if not isinstance(item, dict):
                    continue
                item_title = str(item.get("title") or "").strip()
                if self._title_like_match(title, item_title):
                    tmdb_id = item.get("id")
                    break
            if not tmdb_id:
                return None

            # Query FanArt.tv with TMDB ID
            api_key = self._fanart_api_key or "9ebd4b92ba42e92e4fbc404cbb0c3a72"  # public fallback key
            url = f"https://webservice.fanart.tv/v3/movies/{tmdb_id}?api_key={api_key}"
            data = await self._http.get_json(url)
            if not data or not isinstance(data, dict):
                return None

            # Try movieposter first, then hdmovieclearart
            for key in ("movieposter", "hdmovieclearart", "moviethumb"):
                posters = data.get(key, [])
                if posters and isinstance(posters, list):
                    # Prefer English language posters
                    for p in posters:
                        if isinstance(p, dict) and p.get("lang") in ("en", ""):
                            url_val = p.get("url")
                            if url_val:
                                return {"poster_url": url_val}
                    # Fallback to first available
                    first = posters[0]
                    if isinstance(first, dict) and first.get("url"):
                        return {"poster_url": first["url"]}
            return None
        except Exception:
            return None

    async def _lookup_letterboxd(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        """Try to find poster from Letterboxd by scraping their film page."""
        try:
            # Create slug from title: "The Matrix" -> "the-matrix"
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            if year:
                # Try with year first: "the-matrix-1999"
                url = f"https://letterboxd.com/film/{slug}-{year}/"
                html = await self._http.get_text(url)
                poster_url = self._extract_letterboxd_poster(html)
                if poster_url:
                    return {"poster_url": poster_url}

            # Try without year
            url = f"https://letterboxd.com/film/{slug}/"
            html = await self._http.get_text(url)
            poster_url = self._extract_letterboxd_poster(html)
            if poster_url:
                return {"poster_url": poster_url}

            return None
        except Exception:
            return None

    @staticmethod
    def _extract_letterboxd_poster(html: str) -> str | None:
        """Extract poster URL from Letterboxd HTML."""
        if not html:
            return None
        # Look for og:image meta tag which has the poster
        match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
        if match:
            url = match.group(1)
            # Letterboxd uses URLs like: https://a.ltrbxd.com/resized/film-poster/...
            if "ltrbxd.com" in url or "letterboxd.com" in url:
                return url
        return None

    async def _lookup_omdb(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        """Try OMDB API (free tier, no key needed for posters)."""
        try:
            params = {"t": title, "type": "movie"}
            if year:
                params["y"] = str(year)
            url = f"https://www.omdbapi.com/?apikey=925eba28&{self._urlencode(params)}"
            data = await self._http.get_json(url)
            if data and data.get("Response") == "True":
                poster = data.get("Poster")
                if poster and poster != "N/A":
                    result = {"poster_url": poster}
                    if data.get("Plot") and data["Plot"] != "N/A":
                        result["overview"] = data["Plot"]
                    if data.get("Genre") and data["Genre"] != "N/A":
                        result["genres"] = [g.strip() for g in data["Genre"].split(",")]
                    return result
            return None
        except Exception:
            return None

    async def _lookup_imdb(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        """Try to find poster from IMDB search."""
        try:
            query = f"{title} {year}" if year else title
            search_url = f"https://www.imdb.com/find/?q={self._urlencode_value(query)}&s=tt&ttype=ft"
            html = await self._http.get_text(search_url)
            if not html:
                return None
            # Find first movie result with poster
            match = re.search(r'<img[^>]+src="(https://m\.media-amazon\.com/images/[^"]+)"', html)
            if match:
                poster_url = match.group(1)
                # Upgrade to larger image
                poster_url = re.sub(r'\._V1_.*\.', '._V1_SX600.', poster_url)
                return {"poster_url": poster_url}
            return None
        except Exception:
            return None

    async def _lookup_wikipedia(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        """Try to find poster from Wikipedia."""
        try:
            # Search Wikipedia for the movie
            query = f"{title} ({year} film)" if year else f"{title} (film)"
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&format=json&prop=pageimages&titles={self._urlencode_value(query)}&pithumbsize=500"
            data = await self._http.get_json(search_url)
            if data:
                pages = data.get("query", {}).get("pages", {})
                for page in pages.values():
                    thumb = page.get("thumbnail", {}).get("source")
                    if thumb:
                        return {"poster_url": thumb}

            # Try without "(year film)" suffix
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&format=json&prop=pageimages&titles={self._urlencode_value(title)}&pithumbsize=500"
            data = await self._http.get_json(search_url)
            if data:
                pages = data.get("query", {}).get("pages", {})
                for page in pages.values():
                    thumb = page.get("thumbnail", {}).get("source")
                    if thumb:
                        return {"poster_url": thumb}
            return None
        except Exception:
            return None

    async def _lookup_rottentomatoes(self, title: str, year: int | None = None) -> dict[str, Any] | None:
        """Try to find poster from Rotten Tomatoes."""
        try:
            # Create slug: "The Matrix" -> "the_matrix"
            slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
            url = f"https://www.rottentomatoes.com/m/{slug}"
            html = await self._http.get_text(url)
            if html:
                # Look for og:image
                match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
                if match:
                    poster_url = match.group(1)
                    if "rottentomatoes.com" in poster_url or "flixster.com" in poster_url:
                        return {"poster_url": poster_url}
            return None
        except Exception:
            return None

    @staticmethod
    def _merge_result(existing: dict | None, new: dict) -> dict:
        """Merge new result into existing, filling missing keys."""
        if not existing:
            return new
        for k, v in new.items():
            if k not in existing:
                existing[k] = v
        return existing

    @staticmethod
    def _urlencode(params: dict) -> str:
        from urllib.parse import urlencode
        return urlencode(params)

    @staticmethod
    def _urlencode_value(value: str) -> str:
        from urllib.parse import quote
        return quote(value)
