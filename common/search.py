"""Vector search over ``policies`` and ``adjudications``.

Prefers Atlas Vector Search with Automated Embeddings. Falls back to a
classic ``$vectorSearch`` (a "bring your own embeddings" index) when
Automated Embeddings aren't available, then to keyword overlap so the
Judge keeps working offline / pre-index.
"""
from __future__ import annotations

import functools
import json
import logging
import os
from pathlib import Path
from typing import Any

from common import db as dbmod

log = logging.getLogger(__name__)

# Atlas Vector Search index names created by scripts/setup_db.py
INDEX_POLICIES = "policies_vector"
INDEX_ADJUDICATIONS = "adjudications_vector"

_TEXT_FIELD = {
    dbmod.POLICIES: "clause_text",
    dbmod.ADJUDICATIONS: "denial_text",
}

_STUB_PATH = Path(__file__).parent / "fixtures" / "search_stub.json"


def vector_search(
    collection: str,
    query: str | None = None,
    filters: dict | None = None,
    k: int = 5,
    *,
    query_text: str | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Return up to ``k`` documents ranked by semantic similarity to ``query``.

    Parameter aliases ``query_text`` / ``top_k`` match the orchestrator contract
    so callers can switch imports with no other change.
    """
    text = query_text if query_text is not None else (query or "")
    limit = top_k if top_k is not None else k
    filters = filters or {}

    try:
        hits = _atlas_vector_search(collection, text, filters, limit)
        if hits:
            return hits
    except Exception as exc:  # noqa: BLE001 — fall through to keyword
        log.debug("Atlas vector search unavailable (%s); using keyword fallback", exc)

    try:
        hits = _keyword_search(collection, text, filters, limit)
        if hits:
            return hits
    except Exception as exc:  # noqa: BLE001
        log.debug("Mongo keyword search failed (%s); using stub fixtures", exc)

    return _stub_results(collection, text, filters, limit)


def _atlas_vector_search(
    collection: str, query_text: str, filters: dict, limit: int
) -> list[dict]:
    """``$vectorSearch`` against an Automated Embeddings index.

    Automated Embeddings means we pass the query string; Atlas embeds it.
    If the cluster only has a classic vector index, this stage will error and
    the caller falls through to keyword search / Embedding API.
    """
    index = INDEX_POLICIES if collection == dbmod.POLICIES else INDEX_ADJUDICATIONS
    path = _TEXT_FIELD.get(collection, "clause_text")
    db = dbmod.get_db("agent_worker")

    filter_stage: dict[str, Any] = dict(filters)
    # Automated Embeddings query form (Atlas embeds the query text).
    auto_pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": index,
                "path": path,
                "query": {"text": query_text},
                "numCandidates": max(limit * 20, 50),
                "limit": limit,
                **({"filter": filter_stage} if filter_stage else {}),
            }
        },
        {"$addFields": {"_score": {"$meta": "vectorSearchScore"}}},
    ]
    try:
        return list(db[collection].aggregate(auto_pipeline))
    except Exception:
        # Classic form expects a queryVector, computed the same way the stored
        # document vectors were (scripts/embed_policies.py) -- a query vector
        # from a different model would compare against a meaningless space.
        vector = _embed(query_text)
        if vector is None:
            raise
        classic = [
            {
                "$vectorSearch": {
                    "index": index,
                    "path": "embedding",
                    "queryVector": vector,
                    "numCandidates": max(limit * 20, 50),
                    "limit": limit,
                    **({"filter": filter_stage} if filter_stage else {}),
                }
            },
            {"$addFields": {"_score": {"$meta": "vectorSearchScore"}}},
        ]
        return list(db[collection].aggregate(classic))


_LOCAL_MODEL_NAME = "all-MiniLM-L6-v2"  # matches scripts/embed_policies.py; 384 dimensions, free, local


@functools.lru_cache(maxsize=1)
def _local_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(_LOCAL_MODEL_NAME)


def _embed(text: str) -> list[float] | None:
    """Query embedding for the classic $vectorSearch path.

    Tries the local, free all-MiniLM-L6-v2 model first -- this MUST be the
    same model the stored document vectors were embedded with
    (scripts/embed_policies.py), not just any embedding source, or
    similarity scores are meaningless. Falls back to the MongoDB Embedding
    API (Voyage) only if sentence-transformers isn't installed; that path
    only makes sense if the documents were embedded via Voyage too.
    """
    try:
        return _local_model().encode(text).tolist()
    except Exception as exc:  # noqa: BLE001 — package missing or model load failed
        log.debug("Local embedding model unavailable (%s); trying the Embedding API", exc)

    api_key = os.environ.get("MONGODB_EMBEDDING_API_KEY") or os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        return None
    try:
        import httpx

        resp = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"input": [text], "model": "voyage-3-lite"},
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Embedding API failed: %s", exc)
        return None


def _keyword_search(
    collection: str, query_text: str, filters: dict, limit: int
) -> list[dict]:
    db = dbmod.get_db("agent_worker")
    candidates = list(db[collection].find(filters).limit(200))
    return _rank_by_overlap(candidates, query_text, collection, limit)


def _rank_by_overlap(
    candidates: list[dict], query_text: str, collection: str, limit: int
) -> list[dict]:
    field = _TEXT_FIELD.get(collection, "clause_text")
    terms = set(query_text.lower().split())

    def score(doc: dict) -> int:
        text = str(doc.get(field) or doc.get("clause_text") or doc.get("denial_text") or "")
        return len(terms & set(text.lower().split()))

    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[:limit]


def _stub_results(
    collection: str, query_text: str, filters: dict, limit: int
) -> list[dict]:
    """Fixed fixtures so the Judge works before Atlas indexes exist."""
    if not _STUB_PATH.exists():
        return []
    data = json.loads(_STUB_PATH.read_text())
    docs = data.get(collection, [])
    insurer = filters.get("insurer")
    if insurer:
        docs = [d for d in docs if d.get("insurer") == insurer]
    if filters.get("current") is True:
        docs = [d for d in docs if d.get("current") is True]
    if filters.get("status"):
        docs = [d for d in docs if d.get("status") == filters["status"]]
    return _rank_by_overlap(docs, query_text, collection, limit)
