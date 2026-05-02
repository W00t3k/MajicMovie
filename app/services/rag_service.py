"""
RAG Service – fully local movie knowledge retrieval.

Stores embeddings in the existing SQLite database (rag_documents table).
No cloud dependencies – uses sentence-transformers all-MiniLM-L6-v2 locally.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import numpy as np

from app.services.embedding import EmbeddingService

logger = logging.getLogger(__name__)

_SOURCE_LABELS: dict[str, str] = {
    "oscars_best_picture": "Oscar Best Picture",
    "criterion_collection": "Criterion Collection",
    "imdb_top250": "IMDb Top 250",
    "rottentomatoes_seed": "Rotten Tomatoes",
    "rogerebert_seed": "Roger Ebert",
    "a24_films": "A24",
    "afi100": "AFI Top 100",
    "cannes_palme_dor": "Cannes Palme d'Or",
    "ghibli_films": "Studio Ghibli",
    "sundance_films": "Sundance",
    "bafta_winners": "BAFTA",
    "golden_globes": "Golden Globes",
    "blumhouse_films": "Blumhouse",
    "marvel_dc": "Marvel/DC",
    "letterboxd_top": "Letterboxd Top",
    "mubi_curated": "MUBI",
    "film_registry": "National Film Registry",
    "metacritic_90": "Metacritic 90+",
    "boxoffice_hits": "Box Office Hits",
    "hidden_gems": "Hidden Gems",
    "directors_spotlight": "Directors Spotlight",
    "decades_essentials": "Decades Essentials",
    "sight_sound_top100": "Sight & Sound Top 100",
    "pixar_films": "Pixar",
    "disney_classics": "Disney Classics",
    "horror_classics": "Horror Classics",
    "scifi_essentials": "Sci-Fi Essentials",
    "anime_essentials": "Anime Essentials",
    "korean_cinema": "Korean Cinema",
    "film_noir": "Film Noir",
    "neon_films": "Neon Films",
}


def _movie_to_text(movie: dict | str, source: str) -> tuple[str, dict]:
    """Convert a movie entry to a rich text chunk + metadata dict."""
    label = _SOURCE_LABELS.get(source, source.replace("_", " ").title())
    if isinstance(movie, str):
        return f"{movie} [{label}]", {"title": movie, "year": None, "source": source, "genres": ""}

    title = str(movie.get("title") or movie.get("name") or "").strip()
    if not title:
        return "", {}

    year = movie.get("year")
    genres: list[str] = movie.get("genres") or []
    overview = str(movie.get("overview") or movie.get("description") or "").strip()
    score = movie.get("tomatometer") or movie.get("score") or movie.get("rt_score") or ""
    director = str(movie.get("director") or "").strip()
    awards = str(movie.get("awards") or movie.get("award") or "").strip()
    note = str(movie.get("note") or "").strip()

    parts = [f"{title}"]
    if year:
        parts.append(f"({year})")
    parts.append(f"[{label}]")
    if director:
        parts.append(f"Director: {director}.")
    if genres:
        parts.append(f"Genres: {', '.join(genres)}.")
    if awards:
        parts.append(f"Awards: {awards}.")
    if score:
        parts.append(f"Score: {score}.")
    if note:
        parts.append(note[:200])
    if overview:
        parts.append(overview[:400])

    text = " ".join(parts)
    meta = {
        "title": title,
        "year": int(year) if year and str(year).isdigit() else None,
        "source": source,
        "genres": ",".join(genres) if genres else "",
    }
    return text, meta


class LocalRAGService:
    """Fully local RAG: SQLite vectors + sentence-transformers embeddings."""

    def __init__(self, db_path: Path, data_dir: Path | None = None):
        self._db_path = db_path
        self._data_dir = data_dir or db_path.parent.parent / "data"
        self._embedder = EmbeddingService()
        self._local = threading.local()

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_table(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                title       TEXT NOT NULL,
                year        INTEGER,
                genres      TEXT,
                chunk_text  TEXT NOT NULL,
                embedding   TEXT NOT NULL,
                indexed_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_source ON rag_documents(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_title  ON rag_documents(title)")
        conn.commit()

    @property
    def doc_count(self) -> int:
        try:
            row = self._connect().execute("SELECT COUNT(*) FROM rag_documents").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def ingest_all(self, force: bool = False) -> dict[str, int]:
        """Read every data/*.json file and upsert embeddings into SQLite."""
        self._init_table()
        conn = self._connect()

        if not force and self.doc_count > 0:
            logger.info("RAG index already populated (%d docs) – skipping ingest", self.doc_count)
            return {"skipped": self.doc_count}

        if force:
            conn.execute("DELETE FROM rag_documents")
            conn.commit()
            logger.info("Cleared existing RAG index for re-ingest")

        json_files = sorted(self._data_dir.glob("*.json"))
        stats: dict[str, int] = {"files": 0, "indexed": 0, "errors": 0}

        all_texts: list[str] = []
        all_meta: list[dict] = []

        for json_file in json_files:
            # Skip seed/cache files that aren't static datasets
            if any(s in json_file.name for s in ("cache", "schedule")):
                continue
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)
                source = json_file.stem
                movies = data if isinstance(data, list) else data.get("movies", [])
                for movie in movies:
                    text, meta = _movie_to_text(movie, source)
                    if text and len(text) > 8:
                        all_texts.append(text)
                        all_meta.append(meta)
                stats["files"] += 1
            except Exception as exc:
                logger.error("Error reading %s: %s", json_file.name, exc)
                stats["errors"] += 1

        if not all_texts:
            return stats

        logger.info("Embedding %d movie chunks …", len(all_texts))
        embeddings = self._embedder.embed_batch(all_texts)

        rows = [
            (
                meta["source"],
                meta["title"],
                meta.get("year"),
                meta.get("genres", ""),
                text[:1200],
                json.dumps(emb),
            )
            for text, meta, emb in zip(all_texts, all_meta, embeddings)
        ]

        conn.executemany(
            "INSERT INTO rag_documents (source, title, year, genres, chunk_text, embedding) VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        stats["indexed"] = len(rows)
        logger.info("RAG index built: %d documents from %d files", len(rows), stats["files"])
        return stats

    def search(
        self,
        query: str,
        limit: int = 6,
        source_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cosine-similarity search over rag_documents."""
        if self.doc_count == 0:
            return []

        q_vec = np.array(self._embedder.embed_sync(query), dtype=np.float32)

        conn = self._connect()
        sql = "SELECT id, source, title, year, genres, chunk_text, embedding FROM rag_documents"
        params: list = []
        if source_filter:
            sql += " WHERE source = ?"
            params.append(source_filter)

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []

        scored: list[tuple[float, dict]] = []
        for row in rows:
            try:
                vec = np.array(json.loads(row["embedding"]), dtype=np.float32)
                norm_q = np.linalg.norm(q_vec)
                norm_v = np.linalg.norm(vec)
                if norm_q == 0 or norm_v == 0:
                    continue
                sim = float(np.dot(q_vec, vec) / (norm_q * norm_v))
                scored.append((sim, {
                    "title": row["title"],
                    "year": row["year"],
                    "source": _SOURCE_LABELS.get(row["source"], row["source"]),
                    "genres": row["genres"],
                    "text": row["chunk_text"],
                    "score": round(sim, 4),
                }))
            except Exception:
                continue

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    async def search_async(self, query: str, limit: int = 6, source_filter: str | None = None) -> list[dict[str, Any]]:
        return self.search(query, limit=limit, source_filter=source_filter)

    def get_movie_context(self, title: str, limit: int = 4) -> str:
        """Return a formatted context block for a given movie title."""
        results = self.search(title, limit=limit)
        if not results:
            return ""
        parts = [f"[{r['source']}] {r['text'][:350]}" for r in results]
        return "\n\n".join(parts)

    async def get_movie_context_async(self, title: str, limit: int = 4) -> str:
        return self.get_movie_context(title, limit=limit)

    def enhance_prompt(self, query: str, limit: int = 4) -> str:
        """Return a RAG context prefix for injecting into an LLM prompt."""
        results = self.search(query, limit=limit)
        if not results:
            return ""
        lines = [f"- {r['title']} ({r['year'] or '?'}): {r['text'][:200]}" for r in results]
        return "Relevant movie knowledge:\n" + "\n".join(lines) + "\n\n"

    async def enhance_prompt_async(self, query: str, limit: int = 4) -> str:
        return self.enhance_prompt(query, limit=limit)


# Backwards-compatible alias so existing imports of RAGService still work
RAGService = LocalRAGService
