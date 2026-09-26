#!/usr/bin/env python3
"""One-off: generate free, local embeddings for `adjudications`, for a
"bring your own embeddings" Atlas Vector Search index (no paid embedding API).

Uses all-MiniLM-L6-v2 (384 dimensions) via sentence-transformers -- runs on
your machine, no API key, no per-call cost. Safe to run more than once
(overwrites each document's `embedding` field).

Embeds `denial_text` when present (the CARC/RARC reason agents/judge.py
compares against policy clauses). Paid claims have no denial_text, so those
fall back to `content_text` (diagnosis + procedure codes) -- judge.py's
"comparable paid claims" search (classify() in agents/judge.py) needs those
embedded too, or paid claims would never turn up as comparables.

    pip install sentence-transformers
    python scripts/embed_adjudications.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentence_transformers import SentenceTransformer  # noqa: E402

from common.db import get_db  # noqa: E402

MODEL_NAME = "all-MiniLM-L6-v2"


def _text_for(doc: dict) -> str:
    return doc.get("denial_text") or doc.get("content_text") or ""


def main() -> None:
    coll = get_db("scorer")["adjudications"]
    docs = [d for d in coll.find({}) if _text_for(d)]
    if not docs:
        sys.exit("No adjudications with denial_text or content_text yet.")

    print(f"Loading {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME)

    print(f"Embedding {len(docs)} adjudications ...")
    texts = [_text_for(d) for d in docs]
    vectors = model.encode(texts, show_progress_bar=True).tolist()

    for doc, vector in zip(docs, vectors):
        coll.update_one({"_id": doc["_id"]}, {"$set": {"embedding": vector}})

    print(f"Done. Each adjudication now has an 'embedding' field ({len(vectors[0])} dimensions).")


if __name__ == "__main__":
    main()
