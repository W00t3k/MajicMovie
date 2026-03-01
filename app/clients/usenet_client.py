from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
from xml.etree import ElementTree

from app.clients.http_client import HTTPClient
from app.config import limits

logger = logging.getLogger(__name__)

# Newznab namespace for extended attributes
NEWZNAB_NS = {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/"}


def _human_size(size_bytes: int | None) -> str | None:
    """Convert bytes to human-readable size string."""
    if size_bytes is None or size_bytes <= 0:
        return None
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units[:-1]:
        if abs(size) < 1024.0:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} {units[-1]}"


class UsenetClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = HTTPClient(timeout_seconds)

    def _api_endpoint(self) -> str:
        if self._base_url.lower().endswith("/api"):
            return self._base_url
        return f"{self._base_url}/api"

    def _search_endpoint_and_params(
        self, query: str = "", offset: int = 0, limit: int = 100, output_format: str = "xml"
    ) -> tuple[str, dict[str, str | int]]:
        parsed = urlparse(self._base_url)
        base_params = dict(parse_qsl(parsed.query, keep_blank_values=True))

        # Strip query/fragment and normalize the API endpoint path.
        endpoint = urlunparse(parsed._replace(query="", fragment="")).rstrip("/")
        if not endpoint.lower().endswith("/api"):
            endpoint = f"{endpoint}/api"

        params: dict[str, str | int] = {
            "t": "search",
            "cat": "2000",  # Movies category (Newznab)
            "o": output_format,  # Use XML to get size info
            "limit": limit,
            "offset": offset,
            "extended": "1",  # Request extended attributes (includes size)
        }
        if query.strip():
            params["q"] = query.strip()
        if self._api_key:
            params["apikey"] = self._api_key

        # Preserve explicit query-string defaults from base_url (for providers that require them).
        params.update(base_params)

        # Explicit function arguments / configured key always win.
        if query.strip():
            params["q"] = query.strip()
        if self._api_key:
            params["apikey"] = self._api_key
        params["limit"] = limit
        params["offset"] = offset

        return endpoint, params

    async def movie_search(
        self, query: str = "", offset: int = 0, limit: int = 100
    ) -> list[dict]:
        endpoint, params = self._search_endpoint_and_params(
            query=query, offset=offset, limit=limit, output_format="xml"
        )
        # Request XML format to get size information
        xml_text = await self._http.get_text(endpoint, params=params)
        return self._parse_rss_items(xml_text)

    @staticmethod
    def _normalize_json_item(item: dict) -> dict:
        """Normalize a JSON item to include size and other fields."""
        # Extract size from various JSON formats
        size_bytes = None

        # Try direct size field
        if "size" in item:
            try:
                size_bytes = int(item["size"])
            except (ValueError, TypeError):
                pass

        # Try enclosure object (some indexers)
        if size_bytes is None and "enclosure" in item:
            enc = item["enclosure"]
            if isinstance(enc, dict):
                try:
                    size_bytes = int(enc.get("length") or enc.get("@length") or 0)
                except (ValueError, TypeError):
                    pass

        # Try attr array (Newznab extended attributes)
        if size_bytes is None and "attr" in item:
            attrs = item["attr"]
            if isinstance(attrs, list):
                for attr in attrs:
                    if isinstance(attr, dict) and attr.get("@name") == "size":
                        try:
                            size_bytes = int(attr.get("@value", 0))
                            break
                        except (ValueError, TypeError):
                            pass
            elif isinstance(attrs, dict) and attrs.get("@name") == "size":
                try:
                    size_bytes = int(attrs.get("@value", 0))
                except (ValueError, TypeError):
                    pass

        # Try newznab:attr format
        if size_bytes is None:
            for key in item:
                if "attr" in key.lower():
                    val = item[key]
                    if isinstance(val, list):
                        for attr in val:
                            if isinstance(attr, dict) and attr.get("name") == "size":
                                try:
                                    size_bytes = int(attr.get("value", 0))
                                    break
                                except (ValueError, TypeError):
                                    pass

        # Add normalized fields
        item["size_bytes"] = size_bytes if size_bytes and size_bytes > 0 else None
        item["size_human"] = _human_size(size_bytes) if size_bytes and size_bytes > 0 else None

        return item

    async def movie_search_all(
        self, query: str = "", max_results: int | None = None, batch_size: int | None = None
    ) -> list[dict]:
        """Fetch ALL search results with pagination.

        Args:
            query: Search query string
            max_results: Maximum results to fetch (defaults to limits.usenet_max)
            batch_size: Results per batch (defaults to limits.usenet_batch_size)
        """
        # Use configurable limits from config
        effective_max = max_results if max_results is not None else limits.usenet_max
        effective_batch = batch_size if batch_size is not None else limits.usenet_batch_size

        all_items: list[dict] = []
        offset = 0
        while len(all_items) < effective_max:
            batch = await self.movie_search(query=query, offset=offset, limit=effective_batch)
            if not batch:
                break
            all_items.extend(batch)
            if len(batch) < effective_batch:
                break
            offset += effective_batch
        return all_items[:effective_max]

    async def movie_rss_feed(self, rss_url: str, api_key: str | None = None) -> list[dict]:
        resolved_url = self._resolve_rss_url(rss_url=rss_url, api_key=api_key or self._api_key)
        logger.info(f"Fetching RSS from: {resolved_url[:80]}...")
        xml_text = await self._http.get_text(resolved_url)
        logger.info(f"Received {len(xml_text)} bytes, starts with: {xml_text[:100]}")
        items = self._parse_rss_items(xml_text)
        logger.info(f"Parsed {len(items)} items from RSS")
        return items

    @staticmethod
    def _resolve_rss_url(rss_url: str, api_key: str | None) -> str:
        url = rss_url.strip()
        if api_key:
            url = url.replace("{API_KEY}", api_key)
            url = url.replace("${API_KEY}", api_key)

            # If the URL does not include an API key placeholder, add it as query.
            if "{API_KEY}" not in rss_url and "${API_KEY}" not in rss_url:
                parsed = urlparse(url)
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                query.setdefault("apikey", api_key)
                url = urlunparse(
                    parsed._replace(query=urlencode(query, doseq=True))
                )

        return url

    @staticmethod
    def _parse_rss_items(xml_text: str) -> list[dict]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as exc:
            # Some indexers return HTML error pages instead of XML
            raise ValueError(f"Invalid XML from indexer: {exc}") from exc

        channel = root.find("channel")
        if channel is None:
            # Try Newznab error response: <error code="..." description="..."/>
            error_el = root.find("error")
            if error_el is not None:
                code = error_el.get("code", "?")
                desc = error_el.get("description", "unknown error")
                raise ValueError(f"Indexer error {code}: {desc}")
            raise ValueError("Invalid RSS payload: missing channel node")

        items: list[dict] = []
        for item in channel.findall("item"):
            title = item.findtext("title", default="").strip()
            if not title:
                continue
            # Standard RSS fields
            link = item.findtext("link", default="").strip() or None
            # NZBGeek new_movies feed uses <movie> as the link
            if not link:
                link = item.findtext("movie", default="").strip() or None

            # Extract size from multiple sources
            size_bytes = UsenetClient._extract_size(item)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "pub_date": item.findtext("pubDate", default="").strip() or None,
                    "description": item.findtext("description", default="").strip() or None,
                    # NZBGeek new_movies feed includes cover art and imdb id
                    "cover_url": item.findtext("coverurl", default="").strip() or None,
                    "imdb_id": item.findtext("imdbid", default="").strip() or None,
                    # Size information
                    "size_bytes": size_bytes,
                    "size_human": _human_size(size_bytes),
                }
            )
        return items

    @staticmethod
    def _extract_size(item: ElementTree.Element) -> int | None:
        """Extract size in bytes from Newznab RSS item.

        Checks multiple sources:
        1. <enclosure length="..."/> attribute
        2. <newznab:attr name="size" value="..."/>
        3. <size>...</size> element
        """
        # Try enclosure length attribute first (most common)
        enclosure = item.find("enclosure")
        if enclosure is not None:
            length_str = enclosure.get("length", "").strip()
            if length_str and length_str.isdigit():
                return int(length_str)

        # Try newznab:attr with name="size"
        for attr in item.findall("newznab:attr", NEWZNAB_NS):
            if attr.get("name") == "size":
                value = attr.get("value", "").strip()
                if value and value.isdigit():
                    return int(value)

        # Try plain <attr name="size"> without namespace (some indexers)
        for attr in item.findall("attr"):
            if attr.get("name") == "size":
                value = attr.get("value", "").strip()
                if value and value.isdigit():
                    return int(value)

        # Try direct <size> element
        size_el = item.findtext("size", default="").strip()
        if size_el and size_el.isdigit():
            return int(size_el)

        return None
