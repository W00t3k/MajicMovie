from __future__ import annotations

import re
import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import state
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

_MOVIE_PATTERNS = [
    re.compile(r'"([^"]{2,60})"\s*\((\d{4})\)'),   # "Title" (Year)
    re.compile(r'\*\*([^*]{2,60})\*\*\s*\((\d{4})\)'),  # **Title** (Year)
    re.compile(r'([A-Z][A-Za-z0-9 \':!,\-]{2,55}?)\s*\((\d{4})\)'),  # Title (Year)
]

_TITLE_NOISE = re.compile(
    r'^(like|such as|including|watch|see|try|check out|recommend|consider|i suggest|i recommend)\s+',
    re.IGNORECASE,
)


def _extract_titles_from_text(text: str) -> list[tuple[str, str | None]]:
    """Extract (title, year_or_None) pairs from LLM response text."""
    seen: set[str] = set()
    results: list[tuple[str, str | None]] = []
    for pattern in _MOVIE_PATTERNS:
        for m in pattern.finditer(text):
            title = _TITLE_NOISE.sub("", m.group(1).strip())
            year = m.group(2) if pattern.groups >= 2 else None
            key = title.lower()
            if key not in seen and 2 < len(title) < 61:
                seen.add(key)
                results.append((title, year))
    return results[:5]


async def _tmdb_lookup(titles: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    """Look up movies on TMDB and return dicts with poster_url."""
    if not settings.tmdb_api_key or not titles:
        return []
    from app.clients.tmdb_client import TMDBClient
    tmdb = TMDBClient(api_key=settings.tmdb_api_key, timeout_seconds=5.0)
    movies: list[dict[str, Any]] = []
    for title, year in titles:
        try:
            results = await tmdb.search_movie(query=title)
            if not results:
                continue
            m = results[0]
            release = m.get("release_date", "")
            movie_year = int(release[:4]) if release and len(release) >= 4 else (int(year) if year else None)
            poster = (
                f"https://image.tmdb.org/t/p/w500{m['poster_path']}"
                if m.get("poster_path") else None
            )
            movies.append({
                "title": m.get("title", title),
                "year": movie_year,
                "poster_url": poster,
                "genres": [],
                "overview": (m.get("overview") or "")[:120],
            })
        except Exception:
            pass
    return movies


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)


class AIChatRequest(BaseModel):
    message: str
    context: str | None = None


class AIChatResponse(BaseModel):
    response: str
    sources_queried: list[str] = Field(default_factory=list)
    movies: list[dict] = Field(default_factory=list)


# ── /api/chat/* ────────────────────────────────────────────────────────────────

@router.get("/api/chat/status")
async def chat_status() -> dict:
    """Check if AI chat is available."""
    llm = await state.get_llm_client()
    if llm and llm.available:
        return {"available": True, "provider": llm.provider}
    return {"available": False, "provider": None}


@router.post("/api/chat")
async def chat_with_ai(payload: ChatRequest) -> dict:
    """Chat with the AI assistant (standalone chat page)."""
    llm = await state.get_llm_client()
    if not llm or not llm.available:
        return {"response": None, "error": "AI not available. Configure Groq or Ollama in Settings."}

    system_prompt = (
        "You are a friendly and knowledgeable movie assistant. You help users discover films, "
        "provide recommendations, discuss cinema, and answer questions about movies, directors, "
        "actors, and the film industry. Keep responses concise but informative. Use a conversational tone. "
        'When mentioning specific movies, always format them as: "Title" (Year). Example: "Inception" (2010).'
    )

    messages = []
    for msg in payload.history[-6:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        messages.append(f"{'User' if role == 'user' else 'Assistant'}: {content}")
    messages.append(f"User: {payload.message}")
    prompt = "\n".join(messages) + "\nAssistant:"

    try:
        response = await llm.generate(prompt=prompt, system=system_prompt, max_tokens=500)
        text = response.strip()
        titles = _extract_titles_from_text(text)
        movies = await _tmdb_lookup(titles)
        return {"response": text, "provider": llm.provider, "movies": movies}
    except Exception as exc:
        return {"response": None, "error": str(exc)}


# ── /api/ai/chat ───────────────────────────────────────────────────────────────

@router.post("/api/ai/chat")
async def ai_chat(payload: AIChatRequest) -> AIChatResponse:
    """Sidebar AI chat – supports Groq and Ollama, with optional usenet search."""
    llm = await state.get_llm_client()
    if not llm or not llm.available:
        return AIChatResponse(
            response="No AI provider is configured. Add a Groq API key or set up Ollama in Settings.",
            sources_queried=[],
        )

    message = payload.message.strip()
    sources_queried: list[str] = []

    usenet_keywords = [
        "drunkenslug", "nzbgeek", "usenet", "nzb", "indexer", "download", "available", "can i get",
    ]
    is_usenet_query = any(kw in message.lower() for kw in usenet_keywords)

    usenet_context = ""
    if is_usenet_query:
        from app.routers.usenet import query_usenet_sources, format_usenet_results
        stop_words = {
            "hey", "hi", "hello", "drunkenslug", "nzbgeek", "usenet", "can", "you", "do", "have",
            "any", "search", "find", "for", "the", "a", "an", "is", "there", "what", "about",
            "got", "looking", "download", "available", "get", "i",
        }
        search_terms = " ".join(
            w for w in message.split() if w.lower().strip("?,!.") not in stop_words
        )
        if search_terms:
            usenet_results = await query_usenet_sources(search_terms)
            sources_queried = [k for k, v in usenet_results.items() if v]
            usenet_context = format_usenet_results(usenet_results)

    rag_context = ""
    if not is_usenet_query and state.rag_service is not None:
        try:
            rag_context = state.rag_service.enhance_prompt(message, limit=4)
        except Exception:
            rag_context = ""

    if is_usenet_query:
        system_prompt = (
            "You are a helpful movie assistant for the Majic Movie Selector app. "
            "You help users find movies on usenet indexers like DrunkenSlug, NZBGeek, and other Newznab sources. "
            "When users ask about movie availability, summarize the search results helpfully. "
            "Be friendly and concise."
        )
    else:
        system_prompt = (
            "You are a knowledgeable movie expert assistant for the Majic Movie Selector app. "
            "You have extensive knowledge about movies, actors, directors, genres, and film history. "
            "When users ask about movies, actors, or recommendations, provide helpful and accurate information. "
            'Always format movie titles as: "Title" (Year). Example: "Inception" (2010). '
            "Be conversational and concise - 2-3 sentences max."
        )

    # Parse context from frontend: "top:Title1 | Title2; mood:cozy; ..."
    top_titles: list[str] = []
    if payload.context and not is_usenet_query:
        for part in payload.context.split(";"):
            part = part.strip()
            if part.startswith("top:"):
                top_titles = [t.strip() for t in part[4:].split("|") if t.strip()]

    user_prompt = message
    if rag_context:
        user_prompt = f"{rag_context}User question: {message}"
    if usenet_context:
        user_prompt = (
            f"User question: {message}\n\n"
            f"Search results from usenet indexers:\n{usenet_context}\n\nSummarize these results for the user."
        )
    if top_titles and not usenet_context:
        # Strip year from label (e.g. "Inception (2010)") for cleaner injection
        import re as _re
        formatted = []
        for label in top_titles:
            m = _re.match(r'^(.+?)\s*\((\d{4})\)\s*$', label)
            if m:
                formatted.append(f'"{m.group(1).strip()}" ({m.group(2)})')
            else:
                formatted.append(f'"{label}"')
        titles_str = ", ".join(formatted)
        user_prompt = (
            f"Available movies to recommend: {titles_str}\n\n"
            f"User: {message}\n\n"
            f"Reply in 1-2 sentences. You MUST name at least one movie using its exact format from the list above."
        )

    try:
        response = await llm.generate(prompt=user_prompt, system=system_prompt, max_tokens=300)
        text = response.strip()
        # Extract movie titles from LLM text and look up TMDB posters
        titles = _extract_titles_from_text(text)
        movies = await _tmdb_lookup(titles) if not is_usenet_query else []
        return AIChatResponse(response=text, sources_queried=sources_queried, movies=movies)
    except Exception as exc:
        return AIChatResponse(
            response=f"Sorry, the AI request failed: {exc}",
            sources_queried=sources_queried,
        )
