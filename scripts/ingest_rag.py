#!/usr/bin/env python3
"""
Ingest all data/*.json files into the local RAG SQLite index.

Usage:
    python scripts/ingest_rag.py           # incremental (skip if already indexed)
    python scripts/ingest_rag.py --force   # wipe and re-index everything
    python scripts/ingest_rag.py --search "great foreign films"  # test a query
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app.config import settings
from app.services.rag_service import LocalRAGService


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local RAG index from data/*.json files")
    parser.add_argument("--force", action="store_true", help="Wipe existing index and re-embed everything")
    parser.add_argument("--search", metavar="QUERY", help="Run a test search after ingesting")
    parser.add_argument("--limit", type=int, default=6, help="Number of results for --search (default 6)")
    args = parser.parse_args()

    db_path = ROOT / settings.memory_db_path
    data_dir = ROOT / settings.data_dir

    print(f"Database : {db_path}")
    print(f"Data dir : {data_dir}")
    print()

    rag = LocalRAGService(db_path=db_path, data_dir=data_dir)

    existing = rag.doc_count
    if existing and not args.force:
        print(f"Index already has {existing:,} documents. Use --force to re-embed.")
    else:
        t0 = time.time()
        stats = rag.ingest_all(force=args.force)
        elapsed = time.time() - t0
        print(f"Done in {elapsed:.1f}s")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        print(f"\nTotal docs in index: {rag.doc_count:,}")

    if args.search:
        print(f"\nSearch: '{args.search}'")
        print("-" * 60)
        results = rag.search(args.search, limit=args.limit)
        if not results:
            print("No results.")
        else:
            for i, r in enumerate(results, 1):
                year = f" ({r['year']})" if r.get("year") else ""
                print(f"{i}. [{r['score']:.3f}] {r['title']}{year}  —  {r['source']}")
                if r.get("genres"):
                    print(f"   Genres: {r['genres']}")
                print(f"   {r['text'][:120]}…")
                print()


if __name__ == "__main__":
    main()
