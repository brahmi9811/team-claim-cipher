#!/usr/bin/env python3
"""One-off: generate free, local embeddings for `policies.clause_text`, for a
"bring your own embeddings" Atlas Vector Search index (no paid embedding API).

Uses all-MiniLM-L6-v2 (384 dimensions) via sentence-transformers -- runs on
your machine, no API key, no per-call cost. Safe to run more than once
(overwrites each document's `embedding` field).

    pip install sentence-transformers
    python scripts/embed_policies.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentence_transformers import SentenceTransformer  # noqa: E402

from common.db import get_db  # noqa: E402

MODEL_NAME = "all-MiniLM-L6-v2"


def main() -> None:
    coll = get_db("forge")["policies"]
    docs = list(coll.find({}))
    if not docs:
        sys.exit("No documents in `policies` yet. Run: python -m forge policies")

    print(f"Loading {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME)

    print(f"Embedding {len(docs)} policy clauses ...")
    texts = [d["clause_text"] for d in docs]
    vectors = model.encode(texts, show_progress_bar=True).tolist()

    for doc, vector in zip(docs, vectors):
        coll.update_one({"_id": doc["_id"]}, {"$set": {"embedding": vector}})

    print(f"Done. Each policy document now has an 'embedding' field ({len(vectors[0])} dimensions).")


if __name__ == "__main__":
    main()
