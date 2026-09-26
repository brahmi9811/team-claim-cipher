"""MongoDB Queryable Encryption setup for ``claims`` and ``phi_tokens``.

Timebox: if QE isn't available on the sandbox by 12:00, fall back to
Client-Side Field Level Encryption (explicit mode). Both use a local
96-byte key file at ``QE_KEY_FILE``.

Only the firewall process should hold the key file. Agent workers connect
with a user that sees ciphertext for these fields.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.encryption import Algorithm, ClientEncryption
from pymongo.encryption_options import AutoEncryptionOpts

from common import db as dbmod

load_dotenv()
log = logging.getLogger(__name__)

QE_KEY_FILE = Path(os.environ.get("QE_KEY_FILE", "./master-key.key"))
CRYPT_SHARED_PATH = os.environ.get("CRYPT_SHARED_PATH", "")
KEY_ALT_NAME = "claim-cipher-data-key"

# Encrypted patient / notes fields on claims
CLAIMS_ENCRYPTED_FIELDS = [
    "patient.name",
    "patient.dob",
    "patient.member_id",  # equality-queryable
    "patient.ssn",
    "patient.address",
    "patient.phone",
    "notes",
]

# Mode chosen at runtime: "queryable" | "csfle" | "none"
_MODE: str | None = None
_ENC_CLAIMS: Any = None  # cached EncryptedCollection for claims
_DATA_KEY_ID: Any = None


def ensure_master_key(path: Path | None = None) -> Path:
    """Create a 96-byte local master key file if it doesn't exist."""
    path = path or QE_KEY_FILE
    if not path.exists():
        path.write_bytes(os.urandom(96))
        log.info("Created local master key at %s", path)
    return path


def _local_provider() -> dict[str, dict[str, bytes]]:
    key_path = ensure_master_key()
    return {"local": {"key": key_path.read_bytes()}}


def _key_vault_namespace() -> str:
    return f"{dbmod.KEY_VAULT_DB}.{dbmod.KEY_VAULT_COLL}"


QUERYABLE_FIELD = "patient.member_id"  # equality-queryable; the rest are encrypted, not queryable


def _claims_encrypted_fields() -> dict[str, Any]:
    """encryptedFields for ``claims``. keyId None: create_encrypted_collection makes one key per field."""
    return {
        "fields": [
            {
                "path": path,
                "bsonType": "string",
                "keyId": None,
                **({"queries": {"queryType": "equality"}} if path == QUERYABLE_FIELD else {}),
            }
            for path in CLAIMS_ENCRYPTED_FIELDS
        ]
    }


def _server_encrypted_fields(db: Any, name: str) -> dict[str, Any] | None:
    """The encryptedFields the server holds for ``name`` (with real keyIds), or None."""
    info = next(iter(db.list_collections(filter={"name": name})), None)
    return ((info or {}).get("options") or {}).get("encryptedFields")


def ensure_encrypted_claims() -> str:
    """Create ``claims`` as a Queryable Encryption collection. Safe to run twice.

    Returns "created", "ok", or "plain_with_data" (an older unencrypted ``claims``
    that already holds documents; it is left alone and must be dropped by hand).
    """
    raw = dbmod.get_raw_db("firewall")
    if dbmod.CLAIMS in raw.list_collection_names():
        if _server_encrypted_fields(raw, dbmod.CLAIMS):
            return "ok"
        if raw[dbmod.CLAIMS].estimated_document_count():
            return "plain_with_data"
        raw.drop_collection(dbmod.CLAIMS)
    with get_client_encryption() as crypt:
        crypt.create_encrypted_collection(raw, dbmod.CLAIMS, _claims_encrypted_fields(), "local")
    return "created"


def _csfle_schema_map(data_key_id: Any) -> dict[str, Any]:
    """Explicit-mode CSFLE schema for the same fields (fallback)."""
    claims_ns = f"{dbmod.DB_NAME}.{dbmod.CLAIMS}"
    encrypt = {
        "bsonType": "object",
        "properties": {
            "patient": {
                "bsonType": "object",
                "properties": {
                    "name": {
                        "encrypt": {
                            "keyId": [data_key_id],
                            "bsonType": "string",
                            "algorithm": Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Deterministic,
                        }
                    },
                    "dob": {
                        "encrypt": {
                            "keyId": [data_key_id],
                            "bsonType": "string",
                            "algorithm": Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random,
                        }
                    },
                    "member_id": {
                        "encrypt": {
                            "keyId": [data_key_id],
                            "bsonType": "string",
                            "algorithm": Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Deterministic,
                        }
                    },
                    "ssn": {
                        "encrypt": {
                            "keyId": [data_key_id],
                            "bsonType": "string",
                            "algorithm": Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random,
                        }
                    },
                    "address": {
                        "encrypt": {
                            "keyId": [data_key_id],
                            "bsonType": "string",
                            "algorithm": Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random,
                        }
                    },
                    "phone": {
                        "encrypt": {
                            "keyId": [data_key_id],
                            "bsonType": "string",
                            "algorithm": Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random,
                        }
                    },
                },
            },
            "notes": {
                "encrypt": {
                    "keyId": [data_key_id],
                    "bsonType": "string",
                    "algorithm": Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random,
                }
            },
        },
    }
    return {claims_ns: encrypt}


def get_or_create_data_key(client: MongoClient | None = None) -> Any:
    """Ensure a data encryption key exists in ``encryption.__keyVault``."""
    global _DATA_KEY_ID
    if _DATA_KEY_ID is not None:
        return _DATA_KEY_ID

    uri = _firewall_uri()
    client = client or MongoClient(uri)
    kms = _local_provider()
    with ClientEncryption(
        kms,
        _key_vault_namespace(),
        client,
        client.codec_options,
    ) as crypt:
        existing = client[dbmod.KEY_VAULT_DB][dbmod.KEY_VAULT_COLL].find_one(
            {"keyAltNames": KEY_ALT_NAME}
        )
        if existing:
            _DATA_KEY_ID = existing["_id"]
            return _DATA_KEY_ID
        _DATA_KEY_ID = crypt.create_data_key("local", key_alt_names=[KEY_ALT_NAME])
        log.info("Created data encryption key %s", _DATA_KEY_ID)
        return _DATA_KEY_ID


def _auto_encryption_opts(*, mode: str) -> AutoEncryptionOpts:
    kms = _local_provider()
    kwargs: dict[str, Any] = {
        "kms_providers": kms,
        "key_vault_namespace": _key_vault_namespace(),
    }
    if CRYPT_SHARED_PATH:
        kwargs["crypt_shared_lib_path"] = CRYPT_SHARED_PATH

    if mode == "queryable":
        # Explicit encryption on write (EncryptedCollection below), automatic decryption on
        # read. Bypassing query analysis means no crypt_shared / mongocryptd is needed.
        kwargs["bypass_query_analysis"] = True
    else:
        data_key = get_or_create_data_key()
        kwargs["schema_map"] = _csfle_schema_map(data_key)

    return AutoEncryptionOpts(**kwargs)


def _firewall_uri() -> str:
    """URI for the firewall/forge process (holds the key; can write encrypted fields)."""
    for key in (
        "MONGODB_URI_FIREWALL",
        "FIREWALL_MONGODB_URI",
        "FORGE_MONGODB_URI",
        "MONGODB_URI",
    ):
        uri = os.environ.get(key, "")
        if uri:
            return uri
    raise RuntimeError(
        "MONGODB_URI is not set. Copy .env.example to .env and fill it in."
    )


def get_encrypted_client(mode: str | None = None) -> MongoClient:
    """Return a MongoClient with auto-encryption enabled.

    ``mode`` is ``"queryable"`` (preferred) or ``"csfle"`` (fallback).
    """
    uri = _firewall_uri()
    chosen = mode or detect_mode()
    if chosen == "none":
        return MongoClient(uri)
    opts = _auto_encryption_opts(mode=chosen)
    return MongoClient(uri, auto_encryption_opts=opts)


def encrypted_collection(name: str = "claims"):
    """Return a pymongo Collection that encrypts PHI fields on write.

    Role A's forge calls this as ``firewall.encrypted_collection("claims")``
    so patient fields land encrypted (Queryable Encryption or CSFLE). Raises
    if encryption is unavailable — forge should use ``--allow-plaintext`` only
    against a local dev database.
    """
    mode = detect_mode()
    if mode == "none":
        raise RuntimeError(
            "Encryption is not configured (need MONGODB_URI, QE_KEY_FILE, and "
            "preferably CRYPT_SHARED_PATH). Use forge --allow-plaintext only "
            "against a local dev database."
        )
    # Ensure a data key exists before the first encrypted write
    if mode == "csfle":
        get_or_create_data_key()
        client = get_encrypted_client(mode=mode)
        return client[dbmod.DB_NAME][name]

    if name != dbmod.CLAIMS:
        raise RuntimeError(f"No Queryable Encryption schema for '{name}'")
    global _ENC_CLAIMS
    if _ENC_CLAIMS is not None:
        return _ENC_CLAIMS
    raw = dbmod.get_raw_db("firewall")
    fields = _server_encrypted_fields(raw, name)
    if not fields:
        raise RuntimeError(
            f"'{name}' is not an encrypted collection yet. Run scripts/setup_db.py "
            "(drop an old unencrypted 'claims' first)."
        )
    client = get_encrypted_client(mode=mode)
    _ENC_CLAIMS = EncryptedCollection(client[dbmod.DB_NAME][name], fields)
    return _ENC_CLAIMS


def _get_path(doc: dict, path: str) -> tuple[dict | None, str]:
    """(parent dict, last key) for a dotted path, or (None, key) if the parent is missing."""
    *parents, last = path.split(".")
    node: Any = doc
    for part in parents:
        node = node.get(part) if isinstance(node, dict) else None
    return (node if isinstance(node, dict) else None), last


class EncryptedCollection:
    """A ``claims`` collection that encrypts PHI fields on write (Queryable Encryption,
    explicit mode) and decrypts them on read. Everything except the write methods
    below is passed straight to the underlying pymongo Collection."""

    def __init__(self, collection: Any, encrypted_fields: dict[str, Any]):
        self._coll = collection
        self._fields = encrypted_fields["fields"]
        self._crypt = _shared_crypt()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._coll, name)

    def encrypt_doc(self, doc: dict) -> dict:
        import copy

        out = copy.deepcopy(doc)
        for field in self._fields:
            parent, key = _get_path(out, field["path"])
            if parent is None or key not in parent:
                continue
            value = parent[key]
            if value is None or type(value).__name__ == "Binary":  # null or already encrypted
                if value is None:
                    del parent[key]  # QE fields must be encrypted or absent
                continue
            if field.get("queries"):
                parent[key] = self._crypt.encrypt(
                    value, Algorithm.INDEXED, key_id=field["keyId"], contention_factor=0
                )
            else:
                parent[key] = self._crypt.encrypt(value, Algorithm.UNINDEXED, key_id=field["keyId"])
        return out

    def insert_one(self, doc: dict, *args: Any, **kwargs: Any) -> Any:
        return self._coll.insert_one(self.encrypt_doc(doc), *args, **kwargs)

    def insert_many(self, docs: Any, *args: Any, **kwargs: Any) -> Any:
        return self._coll.insert_many([self.encrypt_doc(d) for d in docs], *args, **kwargs)

    def replace_one(self, filter: dict, doc: dict, *args: Any, **kwargs: Any) -> Any:  # noqa: A002
        return self._coll.replace_one(filter, self.encrypt_doc(doc), *args, **kwargs)


def encrypt_object(value: dict) -> Any:
    """Encrypt a whole sub-document (e.g. phi_tokens.patient_ref) with the shared data key.

    Uses randomized CSFLE-style encryption, which works in a plain collection and needs
    no schema. Returns the value unchanged when encryption isn't configured.
    """
    if detect_mode() == "none":
        return value
    key_id = get_or_create_data_key()
    return _shared_crypt().encrypt(value, Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random, key_id=key_id)


def detect_mode() -> str:
    """Pick Queryable Encryption, CSFLE, or none.

    Checks cluster version (QE needs 7.0+) and whether crypt_shared loads.
    Result is cached for the process lifetime.
    """
    global _MODE
    if _MODE is not None:
        return _MODE

    try:
        uri = _firewall_uri()
    except RuntimeError:
        _MODE = "none"
        log.warning("No MONGODB_URI — encryption disabled")
        return _MODE

    try:
        ensure_master_key()
        client = MongoClient(uri, serverSelectionTimeoutMS=5_000)
        info = client.server_info()
        version = info.get("version", "0.0.0")
        major = int(version.split(".")[0])
        client.close()

        if major >= 7:
            try:
                # Probe QE by building opts; actual collection create happens in setup_db
                _auto_encryption_opts(mode="queryable")
                _MODE = "queryable"
                log.info("Encryption mode: Queryable Encryption (cluster %s)", version)
                return _MODE
            except Exception as exc:  # noqa: BLE001
                log.warning("QE probe failed (%s); trying CSFLE", exc)

        # CSFLE fallback
        try:
            get_or_create_data_key()
            _MODE = "csfle"
            log.info("Encryption mode: Client-Side Field Level Encryption")
            return _MODE
        except Exception as exc:  # noqa: BLE001
            log.error("CSFLE also failed (%s); encryption disabled", exc)
            _MODE = "none"
            return _MODE
    except Exception as exc:  # noqa: BLE001
        log.error("Could not detect encryption mode (%s); disabled", exc)
        _MODE = "none"
        return _MODE


_SHARED_CRYPT: ClientEncryption | None = None


def _shared_crypt() -> ClientEncryption:
    """One long-lived ClientEncryption for per-claim encrypt/decrypt (each new one opens a client)."""
    global _SHARED_CRYPT
    if _SHARED_CRYPT is None:
        _SHARED_CRYPT = get_client_encryption()
    return _SHARED_CRYPT


def get_client_encryption() -> ClientEncryption:
    """Explicit ClientEncryption handle for encrypt/decrypt of individual values."""
    client = MongoClient(_firewall_uri())
    return ClientEncryption(
        _local_provider(),
        _key_vault_namespace(),
        client,
        client.codec_options,
    )


def encrypt_value(value: str, *, deterministic: bool = False) -> Any:
    """Encrypt a single string (used when auto-encryption isn't on the client)."""
    if detect_mode() == "none":
        return value
    algo = (
        Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Deterministic
        if deterministic
        else Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random
    )
    return _shared_crypt().encrypt(value, algo, key_alt_name=KEY_ALT_NAME)


def decrypt_value(value: Any) -> Any:
    if detect_mode() == "none" or not hasattr(value, "__class__"):
        return value
    # Binary subtype 6 = encrypted
    try:
        return _shared_crypt().decrypt(value)
    except Exception:  # noqa: BLE001
        return value
