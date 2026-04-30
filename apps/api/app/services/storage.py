"""Filesystem-backed storage manager (MVP-0).

Concurrency-safe dedup (8.2a-patch):
  Two concurrent uploads of the same content_hash could race and both observe
  "no existing blob" with the previous SELECT-then-INSERT pattern, then both
  attempt INSERT and one would fail with UniqueViolation. We replace that with
  an INSERT ... ON CONFLICT DO NOTHING using a STABLE conflict target
  (sb_global_uq partial index column set), then SELECT the canonical row.

Schema is unchanged; DEDUP_SCOPE remains 'global' in MVP-0
(tenant_namespace_id IS NULL).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings


def _shard_path(content_hash: str) -> Path:
    """Return STORAGE_LOCAL_ROOT/blobs/{aa}/{bb}/{hash}."""
    root = Path(get_settings().STORAGE_LOCAL_ROOT)
    return root / "blobs" / content_hash[:2] / content_hash[2:4] / content_hash


def _ensure_storage_root() -> None:
    Path(get_settings().STORAGE_LOCAL_ROOT).mkdir(parents=True, exist_ok=True)


def storage_health() -> str:
    _ensure_storage_root()
    root = Path(get_settings().STORAGE_LOCAL_ROOT)
    try:
        with tempfile.NamedTemporaryFile(prefix=".health_probe_", dir=str(root), delete=False) as f:
            f.write(b"ok")
            probe = f.name
        os.remove(probe)
    except Exception:
        return "fail"
    return "ok"


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_blob_atomic(path: Path, data: bytes) -> None:
    """Atomically place 'data' at 'path'. Skip if already present and size matches."""
    if path.exists():
        if path.stat().st_size == len(data):
            return
        raise RuntimeError(f"storage: existing file at {path} has unexpected size; aborting write")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".incoming_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.remove(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _upsert_global_blob(
    conn: Connection,
    *,
    content_hash: str,
    size_bytes: int,
    mime_type: str | None,
    local_path: str,
) -> tuple[uuid.UUID, bool]:
    """Concurrency-safe upsert of a global (DEDUP_SCOPE=global) storage_blobs row.

    We attempt INSERT ... ON CONFLICT DO NOTHING using a WHERE clause matched
    by the partial UNIQUE index sb_global_uq:
      UNIQUE (content_hash, hash_algorithm) WHERE tenant_namespace_id IS NULL.

    Postgres requires the conflict target to be specified. We use the
    explicit INDEX form via ON CONFLICT (..., ...) WHERE ... so that the
    partial index can be matched.

    Returns (blob_id, deduped). 'deduped' is True if the row already existed.
    """
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO storage_blobs (
                id, tenant_namespace_id, content_hash, hash_algorithm,
                size_bytes, mime_type, storage_backend, local_path, bucket, object_key, refcount
            )
            VALUES (
                :id, NULL, :h, 'sha256',
                :sz, :mime, 'local_fs', :lp, NULL, NULL, 0
            )
            ON CONFLICT (content_hash, hash_algorithm) WHERE tenant_namespace_id IS NULL
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "h": content_hash,
            "sz": size_bytes,
            "mime": mime_type,
            "lp": local_path,
        },
    ).first()

    if inserted is not None:
        return uuid.UUID(str(inserted[0])), False

    # Conflict: a row already exists for this (content_hash, sha256, NULL namespace).
    existing = conn.execute(
        text(
            """
            SELECT id, size_bytes
            FROM storage_blobs
            WHERE content_hash = :h
              AND hash_algorithm = 'sha256'
              AND tenant_namespace_id IS NULL
            LIMIT 1
            """
        ),
        {"h": content_hash},
    ).one()
    blob_id = uuid.UUID(str(existing.id))
    if int(existing.size_bytes) != size_bytes:
        raise RuntimeError(
            "storage: hash collision/size mismatch detected on dedup; refusing to proceed"
        )
    return blob_id, True


def store_blob_and_object(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    content_bytes: bytes,
    filename: str,
    mime_type: str | None,
    owner_kind: str,
    owner_id: uuid.UUID,
    object_type: str,
) -> dict:
    """Persist content_bytes on disk and reflect storage_blobs + storage_objects.

    Returns:
      {
        "blob_id": UUID,
        "storage_object_id": UUID,
        "content_hash": str,
        "size_bytes": int,
        "local_path": str,
        "deduped": bool,
      }
    """
    content_hash = compute_sha256(content_bytes)
    size_bytes = len(content_bytes)
    path = _shard_path(content_hash)

    _ensure_storage_root()
    _write_blob_atomic(path, content_bytes)

    blob_id, deduped = _upsert_global_blob(
        conn,
        content_hash=content_hash,
        size_bytes=size_bytes,
        mime_type=mime_type,
        local_path=str(path),
    )

    # Idempotent storage_objects upsert: same logical owner referring to the
    # same blob must not yield two rows. so_owner_uq enforces this; we use
    # ON CONFLICT DO NOTHING and then SELECT the canonical row.
    candidate_obj_id = uuid.uuid4()
    inserted_obj = conn.execute(
        text(
            """
            INSERT INTO storage_objects (
                id, tenant_id, project_id, blob_id,
                object_type, logical_owner_kind, logical_owner_id
            ) VALUES (
                :id, :tenant, :project, :blob,
                :ot, :ok, :oid
            )
            ON CONFLICT (tenant_id, project_id, object_type, logical_owner_kind, logical_owner_id, blob_id)
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_obj_id,
            "tenant": tenant_id,
            "project": project_id,
            "blob": blob_id,
            "ot": object_type,
            "ok": owner_kind,
            "oid": owner_id,
        },
    ).first()

    if inserted_obj is not None:
        storage_object_id = uuid.UUID(str(inserted_obj[0]))
    else:
        existing_obj = conn.execute(
            text(
                """
                SELECT id FROM storage_objects
                WHERE tenant_id = :t
                  AND ((project_id IS NOT DISTINCT FROM :p))
                  AND object_type = :ot
                  AND logical_owner_kind = :ok
                  AND logical_owner_id = :oid
                  AND blob_id = :b
                LIMIT 1
                """
            ),
            {
                "t": tenant_id,
                "p": project_id,
                "ot": object_type,
                "ok": owner_kind,
                "oid": owner_id,
                "b": blob_id,
            },
        ).one()
        storage_object_id = uuid.UUID(str(existing_obj.id))

    return {
        "blob_id": blob_id,
        "storage_object_id": storage_object_id,
        "content_hash": content_hash,
        "size_bytes": size_bytes,
        "local_path": str(path),
        "deduped": deduped,
    }