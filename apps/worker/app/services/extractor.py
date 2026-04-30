"""Deterministic, mock-driven claim extractor (Phase 8.3).

For each task with attached documents, this module:
  - reads document_versions v1 'parsed' and their document_chunks (ordered);
  - applies a deterministic heuristic to extract candidate "factual" sentences
    from each chunk (sentences containing digits OR a quoted span);
  - normalizes the claim text (whitespace + case-folded) and computes a
    canonical_claim_hash (sha256(utf8(canonical_text))) per task scope;
  - upserts logical_claims uniquely keyed by (task_id, canonical_claim_hash);
  - inserts raw_claims (UNIQUE per (logical_claim, chunk, span, extractor));
  - inserts classified_claims with claim_type='factual', domain_tag='general';
  - inserts claim_ledger_entries v1 with state='candidate' (UNIQUE on
    (claim_logical_id, version_no=1));
  - inserts claim_evidence_links between v1 and the chunk's evidence_span.

Idempotent under redelivery: every INSERT uses ON CONFLICT DO NOTHING on the
right unique constraint. The function returns deterministic counts, useful for
audit payloads.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection


EXTRACTOR_NAME = "mvp0_regex_v1"
EXTRACTOR_VERSION = "0.1.0"
CLASSIFIER_NAME = "mvp0_classifier_v1"
CLASSIFIER_VERSION = "0.1.0"


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.!\?])\s+|\n+")
_HAS_NUMBER_RE = re.compile(r"\d")
_HAS_QUOTE_RE = re.compile(r"[\"'“”«»]")


def _split_sentences(text_value: str) -> list[str]:
    """Deterministic sentence splitter for MVP-0.

    Rationale: we don't ship a sentence segmenter; we approximate with
    punctuation- and newline-based splits. Empty fragments are dropped.
    """
    if not text_value:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text_value)
    return [p.strip() for p in parts if p and p.strip()]


def _is_factual_candidate(sentence: str) -> bool:
    """A sentence is a factual candidate if it contains digits or a quoted span."""
    return bool(_HAS_NUMBER_RE.search(sentence) or _HAS_QUOTE_RE.search(sentence))


def _canonicalize(sentence: str) -> tuple[str, str]:
    """Return (canonical_text, canonical_hash).

    canonical_text: lowercase, whitespace collapsed.
    canonical_hash: sha256 of canonical_text utf-8 bytes.
    """
    canonical = " ".join(sentence.lower().split())
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, h


def _iter_attached_chunks(conn: Connection, task_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return ordered chunks attached to the task, with the per-chunk evidence_span.

    In Phase 8.2 we created exactly one evidence_spans row per chunk. We pick the
    earliest-created span deterministically per chunk.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              dc.id              AS chunk_id,
              dc.inline_text     AS chunk_text,
              dv.document_id     AS document_id,
              dv.version_no      AS version_no,
              dc.chunk_index     AS chunk_index,
              es.id              AS span_id,
              es.quote           AS span_quote,
              es.quote_hash      AS span_quote_hash
            FROM task_documents td
            JOIN uploaded_documents ud ON ud.id = td.document_id
            JOIN document_versions  dv ON dv.document_id = ud.id AND dv.version_kind = 'parsed'
            JOIN document_chunks    dc ON dc.document_version_id = dv.id
            JOIN LATERAL (
              SELECT es2.id, es2.quote, es2.quote_hash
              FROM evidence_spans es2
              WHERE es2.document_chunk_id = dc.id
              ORDER BY es2.created_at ASC, es2.id ASC
              LIMIT 1
            ) es ON TRUE
            WHERE td.task_id = :tid
            ORDER BY td.position ASC, dv.version_no ASC, dc.chunk_index ASC
            """
        ),
        {"tid": task_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _upsert_logical_claim(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    canonical_text: str,
    canonical_hash: str,
) -> uuid.UUID:
    """Idempotent upsert keyed by (task_id, canonical_claim_hash)."""
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO logical_claims (
              id, tenant_id, project_id, task_id,
              canonical_claim_text, canonical_claim_hash
            ) VALUES (
              :id, :t, :p, :tid,
              :ct, :ch
            )
            ON CONFLICT (task_id, canonical_claim_hash) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "t": tenant_id,
            "p": project_id,
            "tid": task_id,
            "ct": canonical_text,
            "ch": canonical_hash,
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0]))
    row = conn.execute(
        text(
            "SELECT id FROM logical_claims WHERE task_id = :tid AND canonical_claim_hash = :ch"
        ),
        {"tid": task_id, "ch": canonical_hash},
    ).one()
    return uuid.UUID(str(row[0]))


def _upsert_raw_claim(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    logical_claim_id: uuid.UUID,
    chunk_id: uuid.UUID,
    span_id: uuid.UUID,
    raw_text: str,
) -> tuple[uuid.UUID, bool]:
    """Returns (raw_claim_id, inserted_flag)."""
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO raw_claims (
              id, tenant_id, project_id, task_id,
              logical_claim_id, document_chunk_id, evidence_span_id,
              raw_text, extractor_name, extractor_version
            ) VALUES (
              :id, :t, :p, :tid,
              :lc, :ch, :sp,
              :rt, :en, :ev
            )
            ON CONFLICT (logical_claim_id, document_chunk_id, evidence_span_id, extractor_name, extractor_version)
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "t": tenant_id,
            "p": project_id,
            "tid": task_id,
            "lc": logical_claim_id,
            "ch": chunk_id,
            "sp": span_id,
            "rt": raw_text,
            "en": EXTRACTOR_NAME,
            "ev": EXTRACTOR_VERSION,
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0])), True
    row = conn.execute(
        text(
            """
            SELECT id FROM raw_claims
            WHERE logical_claim_id = :lc
              AND document_chunk_id = :ch
              AND evidence_span_id = :sp
              AND extractor_name = :en
              AND extractor_version = :ev
            """
        ),
        {"lc": logical_claim_id, "ch": chunk_id, "sp": span_id, "en": EXTRACTOR_NAME, "ev": EXTRACTOR_VERSION},
    ).one()
    return uuid.UUID(str(row[0])), False


def _upsert_classified_claim(
    conn: Connection,
    *,
    raw_claim_id: uuid.UUID,
    logical_claim_id: uuid.UUID,
) -> tuple[uuid.UUID, bool]:
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO classified_claims (
              id, raw_claim_id, logical_claim_id,
              claim_type, domain_tag, qualifiers,
              classifier_name, classifier_version
            ) VALUES (
              :id, :rc, :lc,
              'factual', 'general', '{}'::jsonb,
              :cn, :cv
            )
            ON CONFLICT (raw_claim_id, classifier_name, classifier_version)
            DO NOTHING
            RETURNING id
            """
        ),
        {"id": candidate_id, "rc": raw_claim_id, "lc": logical_claim_id, "cn": CLASSIFIER_NAME, "cv": CLASSIFIER_VERSION},
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0])), True
    row = conn.execute(
        text(
            "SELECT id FROM classified_claims WHERE raw_claim_id = :rc AND classifier_name = :cn AND classifier_version = :cv"
        ),
        {"rc": raw_claim_id, "cn": CLASSIFIER_NAME, "cv": CLASSIFIER_VERSION},
    ).one()
    return uuid.UUID(str(row[0])), False


def _upsert_ledger_v1_candidate(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> tuple[uuid.UUID, bool]:
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO claim_ledger_entries (
              id, claim_logical_id, version_no, state,
              support_scope, user_provided_dependency,
              transition_reason, payload
            ) VALUES (
              :id, :lc, 1, 'candidate',
              'supported_by_user_corpus_only', 'supported_by_user_corpus_only',
              'extracted_by_mock_extractor', '{}'::jsonb
            )
            ON CONFLICT (claim_logical_id, version_no) DO NOTHING
            RETURNING id
            """
        ),
        {"id": candidate_id, "lc": claim_logical_id},
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0])), True
    row = conn.execute(
        text(
            "SELECT id FROM claim_ledger_entries WHERE claim_logical_id = :lc AND version_no = 1"
        ),
        {"lc": claim_logical_id},
    ).one()
    return uuid.UUID(str(row[0])), False


def _upsert_evidence_link_for_v1(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> bool:
    res = conn.execute(
        text(
            """
            INSERT INTO claim_evidence_links (
              id, claim_logical_id, claim_ledger_entry_id, evidence_span_id, link_role
            ) VALUES (
              :id, :lc, :le, :es, 'primary_support'
            )
            ON CONFLICT (claim_ledger_entry_id, evidence_span_id) DO NOTHING
            RETURNING id
            """
        ),
        {"id": uuid.uuid4(), "lc": claim_logical_id, "le": claim_ledger_entry_id, "es": evidence_span_id},
    ).first()
    return res is not None


def run_extraction(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> dict[str, int]:
    """Run the deterministic extraction pipeline. Idempotent.

    Returns counts dict with keys:
      raw_claims_created, raw_claims_total,
      classified_claims_created, classified_claims_total,
      ledger_v1_created, ledger_v1_total,
      logical_claims_total
    """
    chunks = _iter_attached_chunks(conn, task_id)

    raw_created = 0
    cls_created = 0
    ledger_created = 0
    logical_seen: set[uuid.UUID] = set()

    for ch in chunks:
        chunk_text: str = ch["chunk_text"] or ""
        chunk_id: uuid.UUID = uuid.UUID(str(ch["chunk_id"]))
        span_id: uuid.UUID = uuid.UUID(str(ch["span_id"]))

        for sentence in _split_sentences(chunk_text):
            if not _is_factual_candidate(sentence):
                continue
            canonical_text, canonical_hash = _canonicalize(sentence)
            if not canonical_text:
                continue
            logical_id = _upsert_logical_claim(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                canonical_text=canonical_text,
                canonical_hash=canonical_hash,
            )
            logical_seen.add(logical_id)

            raw_id, raw_inserted = _upsert_raw_claim(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                logical_claim_id=logical_id,
                chunk_id=chunk_id,
                span_id=span_id,
                raw_text=sentence,
            )
            if raw_inserted:
                raw_created += 1

            _, cls_inserted = _upsert_classified_claim(
                conn, raw_claim_id=raw_id, logical_claim_id=logical_id
            )
            if cls_inserted:
                cls_created += 1

            ledger_id, ledger_inserted = _upsert_ledger_v1_candidate(
                conn, claim_logical_id=logical_id
            )
            if ledger_inserted:
                ledger_created += 1

            _upsert_evidence_link_for_v1(
                conn,
                claim_logical_id=logical_id,
                claim_ledger_entry_id=ledger_id,
                evidence_span_id=span_id,
            )

    raw_total = int(
        conn.execute(
            text("SELECT COUNT(*) FROM raw_claims WHERE task_id = :t"),
            {"t": task_id},
        ).scalar_one()
    )
    cls_total = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM classified_claims cc
                JOIN raw_claims rc ON rc.id = cc.raw_claim_id
                WHERE rc.task_id = :t
                """
            ),
            {"t": task_id},
        ).scalar_one()
    )
    ledger_total = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM claim_ledger_entries cle
                JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                WHERE lc.task_id = :t AND cle.version_no = 1
                """
            ),
            {"t": task_id},
        ).scalar_one()
    )

    return {
        "raw_claims_created": raw_created,
        "raw_claims_total": raw_total,
        "classified_claims_created": cls_created,
        "classified_claims_total": cls_total,
        "ledger_v1_created": ledger_created,
        "ledger_v1_total": ledger_total,
        "logical_claims_total": len(logical_seen),
    }