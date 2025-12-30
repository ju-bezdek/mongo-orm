import hashlib
import logging
import os
from typing import TYPE_CHECKING, List, Literal, Type
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo.collection import Collection

if TYPE_CHECKING:
    from .core import BaseEntity, IndexSpec

LOCK_FILE = "mongo-orm.lock"


def _get_index_hash(collection_name: str, spec: "IndexSpec") -> str:
    """Generate a stable hash for a collection + index specification."""
    keys = spec.get("keys") or spec.get("key")
    # Normalize keys to a stable tuple representation
    if isinstance(keys, dict):
        # Keep order as provided in dict
        norm_keys = tuple((str(k), float(v)) for k, v in keys.items())
    elif isinstance(keys, list):
        norm_keys = tuple((str(k), float(v)) for k, v in keys)
    else:
        norm_keys = ((str(keys), 1.0),)

    # Create a stable representation of the spec including important options
    relevant_parts = {
        "collection": collection_name,
        "keys": norm_keys,
        "unique": spec.get("unique", False),
        "sparse": spec.get("sparse", False),
        "partialFilterExpression": spec.get("partialFilterExpression"),
        "expireAfterSeconds": spec.get("expireAfterSeconds")
        or spec.get("expire_after_seconds"),
    }
    # Sort top-level keys of relevant_parts to ensure stable string representation
    spec_str = str(sorted(relevant_parts.items(), key=lambda x: x[0]))
    return hashlib.sha256(spec_str.encode()).hexdigest()


def index_exists(collection: Collection, spec: "IndexSpec") -> bool:
    """Return True if an equivalent index already exists in `collection` (sync)."""

    # Normalize target keys to a list of tuples
    keys = spec.get("keys") or spec.get("key")
    if isinstance(keys, dict):
        target_key = list(keys.items())
    elif isinstance(keys, list):
        target_key = keys
    else:
        target_key = [(keys, 1)]

    # Normalize values to floats for comparison
    target_key = [(str(k), float(v)) for k, v in target_key]

    existing_indexes = collection.index_information()

    for name, info in existing_indexes.items():
        # info['key'] can be a list of tuples or a SON/dict object
        raw_existing_key = info["key"]
        if hasattr(raw_existing_key, "items"):
            existing_key = [(str(k), float(v)) for k, v in raw_existing_key.items()]
        else:
            existing_key = [(str(k), float(v)) for k, v in raw_existing_key]

        if existing_key == target_key:
            return True
    return False


def apply_indexes(cls: Type["BaseEntity"], mode: Literal["auto-lock", "always"] = None):
    """Check and apply indexes for a specific entity class."""
    from .core import Config

    mode = mode or Config.MONGODB_INDEX_AUTOAPPLY()
    if not mode or mode == "never":
        return

    indexes = getattr(cls, "__indexes__", None)
    if not indexes:
        return

    collection = cls.get_collection()
    collection_name = cls.__collection_name__

    applied_hashes = set()
    if mode == "auto-lock" and os.path.exists(LOCK_FILE):
        with open(LOCK_FILE, "r") as f:
            applied_hashes = {line.strip() for line in f if line.strip()}

    new_hashes = []

    for spec in indexes:
        idx_hash = _get_index_hash(collection_name, spec)

        if mode == "auto-lock" and idx_hash in applied_hashes:
            continue

        if not index_exists(collection, spec):
            # Create index
            keys = spec.get("keys") or spec.get("key")
            options = {
                k: v
                for k, v in spec.items()
                if k not in ("keys", "key", "expire_after_seconds")
            }
            if "expire_after_seconds" in spec:
                options["expireAfterSeconds"] = spec["expire_after_seconds"]

            try:
                collection.create_index(keys, **options)
            except Exception as e:
                logging.error(
                    f"Failed to create index {keys} on {collection_name}: {e}"
                )

        if mode == "auto-lock":
            new_hashes.append(idx_hash)

    if new_hashes:
        with open(LOCK_FILE, "a") as f:
            for h in new_hashes:
                f.write(f"{h}\n")


def apply_all_indexes(mode: str = None):
    """Apply indexes for all registered entity classes."""
    from .core import Config, Repository

    if mode is None:
        mode = Config.MONGODB_INDEX_AUTOAPPLY()

    if not mode or mode == "never":
        return

    for cls in Repository.all_entities:
        apply_indexes(cls, mode)
