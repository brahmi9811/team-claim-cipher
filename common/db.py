"""MongoDB connection helpers. Collection name constants live here so every
caller uses the same strings.

Roles (when the sandbox allows custom DB users):
  agent_worker  – app collections, no sim_truth, no key vault
  firewall      – adds key vault + encrypted field access
  simulator     – writes sim_truth
  scorer        – reads sim_truth
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database

load_dotenv()

DB_NAME = os.environ.get("MONGODB_DB", "denialfighter")

# Collection names — single source of truth
CLAIMS = "claims"
PHI_TOKENS = "phi_tokens"
PHI_INCIDENTS = "phi_incidents"
POLICIES = "policies"
ADJUDICATIONS = "adjudications"
RULES = "rules"
APPEALS = "appeals"
HARNESS_PROFILES = "harness_profiles"
HARNESS_EVENTS = "harness_events"
METRICS = "metrics"
SIM_TRUTH = "sim_truth"
LLM_CALLS = "llm_calls"  # one document per LLM call: tokens and cost (scorer: cost per claim)
KEY_VAULT_DB = "encryption"
KEY_VAULT_COLL = "__keyVault"

ALL_APP_COLLECTIONS = (
    CLAIMS,
    PHI_TOKENS,
    PHI_INCIDENTS,
    POLICIES,
    ADJUDICATIONS,
    RULES,
    APPEALS,
    HARNESS_PROFILES,
    HARNESS_EVENTS,
    METRICS,
    SIM_TRUTH,
    LLM_CALLS,
)

# Env var names for per-role URIs (B names + A's FORGE_/SIM_/SCORER_ aliases).
# First match wins; then fall back to MONGODB_URI.
_ROLE_URI_ENV: dict[str, tuple[str, ...]] = {
    "agent_worker": ("MONGODB_URI_AGENT", "AGENT_MONGODB_URI"),
    "firewall": ("MONGODB_URI_FIREWALL", "FIREWALL_MONGODB_URI"),
    "simulator": ("MONGODB_URI_SIMULATOR", "SIM_MONGODB_URI"),
    "scorer": ("MONGODB_URI_SCORER", "SCORER_MONGODB_URI"),
    # Forge writes encrypted claims + phi canaries into sim_truth
    "forge": ("FORGE_MONGODB_URI", "MONGODB_URI_FIREWALL", "FIREWALL_MONGODB_URI"),
}

# Collections each role must never touch (enforced in code when Atlas roles
# aren't available on the sandbox tier).
_ROLE_DENY: dict[str, frozenset[str]] = {
    "agent_worker": frozenset({SIM_TRUTH, f"{KEY_VAULT_DB}.{KEY_VAULT_COLL}"}),
    "firewall": frozenset({SIM_TRUTH}),
    "simulator": frozenset(),
    "scorer": frozenset(),
    "forge": frozenset({f"{KEY_VAULT_DB}.{KEY_VAULT_COLL}"}),
}


class AccessDenied(PermissionError):
    """Raised when a role tries to read a collection it shouldn't."""


class GuardedDatabase:
    """Thin wrapper that blocks forbidden collections at the Python layer.

    Real Atlas custom roles are preferred; this is the sandbox fallback so
    we can still say honestly that agents cannot read ``sim_truth``.
    """

    def __init__(self, db: Database, role: str):
        self._db = db
        self._role = role
        self._deny = _ROLE_DENY.get(role, frozenset())

    @property
    def name(self) -> str:
        return self._db.name

    @property
    def client(self) -> MongoClient:
        return self._db.client

    def __getitem__(self, name: str) -> Any:
        if name in self._deny:
            raise AccessDenied(f"role '{self._role}' cannot access collection '{name}'")
        return self._db[name]

    def get_collection(self, name: str, **kwargs: Any) -> Any:
        if name in self._deny:
            raise AccessDenied(f"role '{self._role}' cannot access collection '{name}'")
        return self._db.get_collection(name, **kwargs)

    def list_collection_names(self, **kwargs: Any) -> list[str]:
        return self._db.list_collection_names(**kwargs)

    def create_collection(self, name: str, **kwargs: Any) -> Any:
        return self._db.create_collection(name, **kwargs)

    def command(self, *args: Any, **kwargs: Any) -> Any:
        return self._db.command(*args, **kwargs)

    def with_options(self, **kwargs: Any) -> GuardedDatabase:
        return GuardedDatabase(self._db.with_options(**kwargs), self._role)


def _uri_for_role(role: str) -> str:
    for key in _ROLE_URI_ENV.get(role, ()):
        specific = os.environ.get(key, "")
        if specific:
            return specific
    uri = os.environ.get("MONGODB_URI", "")
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set. Copy .env.example to .env and fill it in."
        )
    return uri


@lru_cache(maxsize=8)
def _client_for(uri: str) -> MongoClient:
    return MongoClient(uri, serverSelectionTimeoutMS=8_000)


def get_client(role: str = "agent_worker") -> MongoClient:
    return _client_for(_uri_for_role(role))


def get_db(role: str = "agent_worker") -> GuardedDatabase:
    """Return the ``denialfighter`` database for ``role``.

    Pass ``role="firewall"`` when you need the key vault / encrypted fields.
    Pass ``role="scorer"`` only from the scorer process.
    """
    client = get_client(role)
    return GuardedDatabase(client[DB_NAME], role)


def get_raw_db(role: str = "agent_worker") -> Database:
    """Unwrapped pymongo Database — used by setup scripts and encryption."""
    return get_client(role)[DB_NAME]


def ping(role: str = "agent_worker") -> bool:
    try:
        get_client(role).admin.command("ping")
        return True
    except Exception:
        return False
