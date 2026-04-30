"""Document upload endpoints (8.2).

Accepts .txt and .md (max 50 MiB), persists blob via Storage Manager, creates
uploaded_documents + a single 'parsed' document_versions, splits the text into
ordered chunks (max 4000 chars each), and creates one evidence_spans per chunk.

Audit:
  - 'document.uploaded' on chain_scope='project' (scope_id=project_id).

NOTE: in MVP-0 we are in Closed Corpus only. Documents are tier='user_provided'.
No prompt-injection scanning is performed in 8.2: prompt_injection_flags is
declared by the schema but populated by the worker in later phases.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Path, UploadFile
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.db.audit import audit_append
from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import DocumentChunkRead, DocumentRead

from ..config import get_settings
from ..db import get_conn, transaction
from ..services.storage import store_blob_and_object


router = APIRouter(prefix="/api/v1", tags=["documents"])


MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MiB
ALLOWED_EXTENSIONS = {"txt", "md"}
ALLOWED_MIME_PREFIXES = ("text/",)
MIME_BY_EXT = {"txt": "text/plain", "md": "text/markdown"}
MAX_CHUNK_CHARS = 4000
QUOTE_TRUNCATE_AT = 500


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _row_to_document(row: Any) -> DocumentRead:
    return DocumentRead(
        id=uuid.UUID(str(row.id)),
        tenant_id=uuid.UUID(str(row.tenant_id)),
        project_id=uuid.UUID(str(row.project_id)),
        storage_object_id=uuid.UUID(str(row.storage_object_id)),
        filename=row.filename,
        content_hash=row.content_hash,
        mime_type=row.mime_type,
        size_bytes=int(row.size_bytes),
        tier=row.tier,
        language=row.language,
        created_by=uuid.UUID(str(row.created_by)) if row.created_by else None,
        created_at=row.created_at,
    )


def _detect_language(text_value: str) -> str:
    """Cheap heuristic: returns 'it', 'en', or 'und'.

    Avoids any external library. The signal is a small set of stop-words.
    For MVP-0 this is good enough; misclassification is safe because language
    only affects later renderer choices, never claim verification.
    """
    sample = text_value.lower()[:5000]
    it_hits = sum(sample.count(w) for w in (" il ", " la ", " che ", " e ", " di "))
    en_hits = sum(sample.count(w) for w in (" the ", " and ", " of ", " is ", " to "))
    if it_hits == 0 and en_hits == 0:
        return "und"
    return "it" if it_hits > en_hits else "en"


def _chunk_text(text_value: str, max_chars: int = MAX_CHUNK_CHARS) -> list[tuple[int, int, str]]:
    """Split text into (char_start, char_end, chunk_text), preserving order.

    Splits on hard boundaries to keep MVP-0 deterministic. The output is
    contiguous: chunks[i].char_end == chunks[i+1].char_start.
    """
    if not text_value:
        return []
    out: list[tuple[int, int, str]] = []
    pos = 0
    n = len(text_value)
    while pos < n:
        end = min(pos + max_chars, n)
        out.append((pos, end, text_value[pos:end]))
        pos = end
    return out


def _resolve_dev_user(conn: Connection, tenant_id: uuid.UUID) -> uuid.UUID | None:
    s = get_settings()
    user = conn.execute(
        text("SELECT id FROM users WHERE tenant_id = :t AND email = :e"),
        {"t": tenant_id, "e": s.DEV_USER_EMAIL},
    ).first()
    return uuid.UUID(str(user[0])) if user else None


@router.post(
    "/projects/{project_id}/documents",
    status_code=201,
    response_model=DocumentRead,
)
async def upload_document(
    project_id: uuid.UUID = Path(...),
    file: UploadFile = File(...),
) -> DocumentRead:
    # Read upfront to enforce the size cap deterministically.
    raw = await file.read()
    size = len(raw)
    if size > MAX_UPLOAD_BYTES:
        raise NormalizedError(
            ErrorCode.STORAGE_INLINE_TOO_LARGE,
            f"File too large: {size} bytes (max {MAX_UPLOAD_BYTES}).",
        )
    if size == 0:
        raise NormalizedError(ErrorCode.VALIDATION_ERROR, "Empty file is not allowed.")

    filename = file.filename or "upload"
    ext = _ext(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise NormalizedError(
            ErrorCode.VALIDATION_ERROR,
            f"Unsupported file extension {ext!r}. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    # MIME validation: trust the extension, but if the client provided a content_type,
    # accept any 'text/*' or the canonical MIME for the extension.
    mime_type = file.content_type or MIME_BY_EXT[ext]
    if not (mime_type.startswith(ALLOWED_MIME_PREFIXES) or mime_type == MIME_BY_EXT[ext]):
        raise NormalizedError(
            ErrorCode.VALIDATION_ERROR,
            f"Unsupported MIME type {mime_type!r}. Allowed prefixes: {ALLOWED_MIME_PREFIXES}",
        )

    # Decode once to text. .txt/.md are expected to be UTF-8; replace errors keep the
    # ingestion robust without silently dropping content.
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("utf-8", errors="replace")

    text_hash = hashlib.sha256(decoded.encode("utf-8")).hexdigest()
    language = _detect_language(decoded)

    with transaction() as conn:
        project = conn.execute(
            text(
                "SELECT id, tenant_id FROM projects WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": project_id},
        ).first()
        if not project:
            raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Project not found")
        tenant_id = uuid.UUID(str(project.tenant_id))
        user_id = _resolve_dev_user(conn, tenant_id)

        document_id = uuid.uuid4()

        store_result = store_blob_and_object(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            content_bytes=raw,
            filename=filename,
            mime_type=mime_type,
            owner_kind="uploaded_document",
            owner_id=document_id,
            object_type="upload",
        )

        conn.execute(
            text(
                """
                INSERT INTO uploaded_documents (
                    id, tenant_id, project_id, storage_object_id,
                    filename, content_hash, mime_type, size_bytes,
                    tier, language, created_by
                ) VALUES (
                    :id, :tenant, :project, :so,
                    :fn, :h, :mime, :sz,
                    'user_provided', :lang, :uid
                )
                """
            ),
            {
                "id": document_id,
                "tenant": tenant_id,
                "project": project_id,
                "so": store_result["storage_object_id"],
                "fn": filename,
                "h": store_result["content_hash"],
                "mime": mime_type,
                "sz": store_result["size_bytes"],
                "lang": language,
                "uid": user_id,
            },
        )

        # document_versions: a single 'parsed' v1.
        inline_text = decoded if len(decoded.encode("utf-8")) <= 65536 else None
        version_id = uuid.uuid4()
        conn.execute(
            text(
                """
                INSERT INTO document_versions (
                    id, document_id, version_no, version_kind,
                    storage_object_id, inline_text, text_hash
                ) VALUES (
                    :id, :did, 1, 'parsed',
                    :so, :it, :th
                )
                """
            ),
            {
                "id": version_id,
                "did": document_id,
                "so": store_result["storage_object_id"],
                "it": inline_text,
                "th": text_hash,
            },
        )

        # document_chunks: deterministic split.
        chunks = _chunk_text(decoded, MAX_CHUNK_CHARS)
        chunk_ids: list[uuid.UUID] = []
        for idx, (cs, ce, ctext) in enumerate(chunks):
            cid = uuid.uuid4()
            chunk_ids.append(cid)
            conn.execute(
                text(
                    """
                    INSERT INTO document_chunks (
                        id, document_version_id, source_version_id,
                        chunk_index, char_start, char_end, inline_text, text_hash
                    ) VALUES (
                        :id, :dv, NULL,
                        :ix, :cs, :ce, :it, :th
                    )
                    """
                ),
                {
                    "id": cid,
                    "dv": version_id,
                    "ix": idx,
                    "cs": cs,
                    "ce": ce,
                    "it": ctext,
                    "th": hashlib.sha256(ctext.encode("utf-8")).hexdigest(),
                },
            )
            # evidence_spans: one safe-truncated quote per chunk.
            quote = ctext[:QUOTE_TRUNCATE_AT]
            conn.execute(
                text(
                    """
                    INSERT INTO evidence_spans (
                        id, document_chunk_id, char_start, char_end, quote, quote_hash
                    ) VALUES (
                        :id, :cid, 0, :ce, :q, :qh
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "cid": cid,
                    "ce": len(quote),
                    "q": quote,
                    "qh": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                },
            )

        audit_append(
            conn,
            chain_scope="project",
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=None,
            session_id=None,
            event_type="document.uploaded",
            actor_type="user" if user_id else "system",
            actor_id=str(user_id) if user_id else "system",
            redacted_payload={
                "document_id": str(document_id),
                "filename": filename,
                "size_bytes": size,
                "content_hash": store_result["content_hash"],
                "deduped": store_result["deduped"],
                "language": language,
                "chunk_count": len(chunks),
            },
            related_entity_type="uploaded_documents",
            related_entity_id=document_id,
        )

        row = conn.execute(
            text(
                """
                SELECT id, tenant_id, project_id, storage_object_id,
                       filename, content_hash, mime_type, size_bytes,
                       tier, language, created_by, created_at
                FROM uploaded_documents WHERE id = :id
                """
            ),
            {"id": document_id},
        ).one()

    return _row_to_document(row)


@router.get("/documents/{document_id}", response_model=DocumentRead)
def get_document(document_id: uuid.UUID, conn: Connection = Depends(get_conn)) -> DocumentRead:
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, project_id, storage_object_id,
                   filename, content_hash, mime_type, size_bytes,
                   tier, language, created_by, created_at
            FROM uploaded_documents WHERE id = :id
            """
        ),
        {"id": document_id},
    ).first()
    if not row:
        raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Document not found")
    return _row_to_document(row)


@router.get("/projects/{project_id}/documents")
def list_project_documents(
    project_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = 50,
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        raise NormalizedError(ErrorCode.VALIDATION_ERROR, "limit must be in [1, 200]")
    rows = conn.execute(
        text(
            """
            SELECT id, tenant_id, project_id, storage_object_id,
                   filename, content_hash, mime_type, size_bytes,
                   tier, language, created_by, created_at
            FROM uploaded_documents
            WHERE project_id = :pid
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """
        ),
        {"pid": project_id, "limit": limit},
    ).fetchall()
    return {"items": [_row_to_document(r).model_dump(mode="json") for r in rows]}


@router.get("/documents/{document_id}/chunks")
def list_document_chunks(
    document_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = 200,
) -> dict[str, Any]:
    if limit < 1 or limit > 2000:
        raise NormalizedError(ErrorCode.VALIDATION_ERROR, "limit must be in [1, 2000]")
    rows = conn.execute(
        text(
            """
            SELECT dc.id, dc.document_version_id, dc.chunk_index,
                   dc.char_start, dc.char_end, dc.text_hash, dc.inline_text, dc.created_at
            FROM document_chunks dc
            JOIN document_versions dv ON dv.id = dc.document_version_id
            WHERE dv.document_id = :did
            ORDER BY dv.version_no ASC, dc.chunk_index ASC
            LIMIT :limit
            """
        ),
        {"did": document_id, "limit": limit},
    ).fetchall()
    out = [
        DocumentChunkRead(
            id=uuid.UUID(str(r.id)),
            document_version_id=uuid.UUID(str(r.document_version_id)),
            chunk_index=int(r.chunk_index),
            char_start=int(r.char_start),
            char_end=int(r.char_end),
            text_hash=r.text_hash,
            inline_text=r.inline_text,
            created_at=r.created_at,
        ).model_dump(mode="json")
        for r in rows
    ]
    return {"items": out}