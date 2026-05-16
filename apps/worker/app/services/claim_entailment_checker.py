"""Mock claim entailment checker service (Phase 8.8A — Block SERVICE).

This module is the FIRST writer for the claim_entailment_checks table
introduced by migration 0009_claim_entailment_checks.sql. It is a
deterministic, mock-driven entailment checker: it does NOT consult any
AI/LLM provider, does NOT perform web search, does NOT run a real NLI
model, and does NOT compute embeddings. It writes explainable,
codomain-valid rows that future blocks (8.8A-ORCHESTRATOR, 8.8A-GATE,
8.8A-READ) can consume.

Strict scope (Phase 8.8A-SERVICE invariants — see PHASE_8_8A_PRE.md
§3, §4):

  - This service ONLY writes to claim_entailment_checks.
  - It does NOT mutate claim_ledger_entries, claim_lineage,
    claim_evidence_links, verification_records, logical_claims.
  - It does NOT mutate final_gate_reports, draft_final_answers,
    final_answer_spans, final_answer_span_claim_links,
    coverage_gap_statements, published_answers.
  - It does NOT mutate source_quality_assessments,
    source_loss_events, source_loss_propagation_records,
    published_answer_lifecycle_events.
  - It does NOT emit audit_records. The audit emission for claim
    entailment is deferred to the worker integration block
    (8.8A-ORCHESTRATOR / worker integration), which will chain this
    service into the task.created pipeline behind a SAVEPOINT.
  - It does NOT use Redis, does NOT import FastAPI / API modules,
    does NOT perform any network I/O.
  - It does NOT pretend to evaluate real semantic entailment. Every
    row it writes carries ``payload.mock = true`` and a
    ``payload.semantic_warning`` documenting the limitation.

Semantic invariants (from PHASE_8_8A_PRE.md §3, §4):

  - claim entailment != claim correctness.
    A verdict of 'entailed' means the quote supports the claim, NOT
    that the claim is true in the world.
  - claim entailment != evidence support.
    A claim_evidence_links row is a structural link; this table
    evaluates whether the link is semantically justified by the
    quote.
  - claim entailment != CVE-lite verification.
    CVE-lite (verification_records, check_kind='cve_lite') checks
    that the quote is textually present in the document chunk and
    that the quote_hash matches. This service answers a separate
    question: GIVEN that the quote is present, does the quote IMPLY
    the claim?
  - claim entailment != source quality.
    source_quality_assessments (0007) judges the SOURCE that hosts
    the quote. This service judges the RELATION between the claim
    and the quote.
  - claim entailment != contradiction detection.
    A 'contradicted' verdict here would be a LOCAL signal on a
    single (claim, evidence_span) pair. Cross-source contradictions
    are out of scope and belong to a future Contradiction Detector
    (Phase 8.8C). The MVP-0 mock checker does NOT emit
    'contradicted' verdicts — it is too weak semantically to assert
    contradiction without producing false positives. Only a real
    checker (8.9+) will be allowed to emit that verdict.

Mock deterministic policy (PHASE_8_8A_PRE.md §10 / block prompt §4):

  Three rules applied in order; on the first match the verdict is
  fixed and no further rule is consulted.

    Rule 1 — Containment match.
      If the normalized claim text is a substring of the normalized
      quote text (or vice-versa), or if the two strings are equal
      after normalization, the verdict is 'entailed' with
      confidence 0.8.
      Rationale: in MVP-0 the extractor produces raw_claims FROM
      the quote, so this case is the common one for a well-formed
      pipeline.

    Rule 2 — Numeric mismatch.
      If BOTH claim and quote contain numeric tokens AND the set of
      numbers in the claim differs from the set of numbers in the
      quote, the verdict is 'not_supported' with confidence 0.6.
      Rationale: a claim that asserts a different numeric value
      than the supporting quote is almost certainly unsupported.

    Default.
      Verdict is 'uncertain' with confidence 0.5.
      Rationale: the mock heuristic is too weak to assert anything
      stronger.

  The mock does NOT emit 'contradicted' and does NOT emit
  'partially_supported'. Both are reserved for future real checkers
  and for seeded test fixtures.

Versioning and idempotency:

  - version_no is fixed at 1 in MVP-0 (block prompt §6: "non creare
    version_no=2 in questo blocco"). Re-evaluation under a fresh
    idempotency_key against the same (entry, span) pair is therefore
    treated as a programming-error condition and surfaced as
    status='error' rather than silently masked. A future block will
    introduce a real bump strategy.
  - idempotency_key is unique per (claim_ledger_entry_id,
    evidence_span_id, idempotency_key). A redelivery with the same
    key on the same target pair short-circuits to
    status='already_assessed' and returns the existing row id and
    verdict without inserting a duplicate.
  - To prevent races, we acquire a row-level lock on the parent
    claim_ledger_entries row via ``SELECT ... FOR UPDATE`` before
    checking idempotency and INSERTing. This serializes concurrent
    appends for the same (entry, span) pair.

  Belt-and-suspenders: if a concurrent inserter wins the race for
  the same idempotency_key on the same (entry, span) pair despite
  the application-level check, the UNIQUE index
  ``cec_entry_span_idem_uq`` raises an IntegrityError. This service
  catches that specific case, re-reads the existing row, and returns
  status='already_assessed'. If the IntegrityError is instead caused
  by ``cec_entry_span_version_uq`` (a pre-existing v1 with a
  different idempotency_key), the service returns
  status='error' with error_code='entailment_version_conflict' —
  the row is NOT masked.

Transaction model:

  The caller passes an active SQLAlchemy ``Connection`` inside an
  explicit transaction (e.g. ``with engine.begin() as conn:``). This
  module never opens its own connection, never commits, never rolls
  back. The INSERT is wrapped in ``conn.begin_nested()`` so that an
  IntegrityError raised at INSERT time does NOT poison the caller's
  outer transaction.

Canonical scope contract:

  Every row written to claim_entailment_checks uses the canonical
  scope read from the target rows themselves:

    - tenant_id, project_id, task_id come from
      logical_claims (which is, by 0004's design, the structural
      owner of the claim within a task);
    - claim_logical_id, claim_ledger_entry_id, evidence_span_id are
      the inputs from the caller, validated against the DB.

  No caller-supplied tenant/project/task is accepted. This is a
  deliberate divergence from source_quality_evaluator.py (which
  accepts a caller scope and overrides it with the target row's
  scope): for entailment, the orchestrator never has a meaningful
  scope of its own — the scope is dictated by the logical_claim.

Return shape (always):

    {
      "status":                "assessed" | "already_assessed"
                               | "not_found" | "invalid_target"
                               | "error",
      "assessment_id":         str | None,
      "version_no":            int | None,
      "verdict":               str | None,
      "claim_ledger_entry_id": str | None,
      "evidence_span_id":      str | None,
      "tenant_id":             str | None,   # canonical
      "project_id":            str | None,   # canonical
      "task_id":               str | None,   # canonical
      "error_code":            str | None,   # set iff status == 'error'
    }

Identity:

    SERVICE_NAME            = "mvp0_mock_entailment_checker"
    SERVICE_VERSION         = "0.1.0"
    DEFAULT_POLICY_NAME     = "mvp0_mock_entailment"
    DEFAULT_POLICY_VERSION  = "0.1.0"
"""
from __future__ import annotations

import datetime
import json
import re
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evidencefirst_shared.schemas import SOURCE_ENTAILMENT_VERDICT_VALUES


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------
SERVICE_NAME = "mvp0_mock_entailment_checker"
SERVICE_VERSION = "0.1.0"

DEFAULT_CHECKER_NAME = SERVICE_NAME
DEFAULT_CHECKER_VERSION = SERVICE_VERSION
DEFAULT_POLICY_NAME = "mvp0_mock_entailment"
DEFAULT_POLICY_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Result status discriminants
# ---------------------------------------------------------------------------
STATUS_ASSESSED = "assessed"
STATUS_ALREADY_ASSESSED = "already_assessed"
STATUS_NOT_FOUND = "not_found"
STATUS_INVALID_TARGET = "invalid_target"
STATUS_ERROR = "error"


# ---------------------------------------------------------------------------
# Verdict constants (mock heuristic)
# ---------------------------------------------------------------------------
VERDICT_ENTAILED = "entailed"
VERDICT_PARTIALLY_SUPPORTED = "partially_supported"
VERDICT_NOT_SUPPORTED = "not_supported"
VERDICT_CONTRADICTED = "contradicted"
VERDICT_UNCERTAIN = "uncertain"


# Confidence values used by the three deterministic mock rules. Kept as
# module constants so a test can lock them down without grepping the body.
_CONF_ENTAILED_CONTAINMENT = 0.8
_CONF_NOT_SUPPORTED_NUMERIC_MISMATCH = 0.6
_CONF_UNCERTAIN_DEFAULT = 0.5


# Stable semantic-warning string. Tests assert on this constant to
# enforce the "not a real NLI" contract on every emitted row.
_SEMANTIC_WARNING = (
    "mvp0 heuristic; not a real NLI/LLM entailment model"
)


# Number extraction: capture integer or decimal tokens, optionally with a
# trailing percent sign. Same definition is used on both claim and quote
# to make the comparison symmetric.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")


# ---------------------------------------------------------------------------
# Codomain validation at module load time
# ---------------------------------------------------------------------------
def _validate_codomain_membership() -> None:
    """Assert that every verdict constant belongs to its DB-side codomain.

    Same pattern used by source_quality_evaluator.py: if a future
    migration shrinks the codomain and this module is not updated, the
    failure surfaces at import time rather than at the next INSERT.
    """
    for v in (
        VERDICT_ENTAILED,
        VERDICT_PARTIALLY_SUPPORTED,
        VERDICT_NOT_SUPPORTED,
        VERDICT_CONTRADICTED,
        VERDICT_UNCERTAIN,
    ):
        assert v in SOURCE_ENTAILMENT_VERDICT_VALUES, (
            f"mock verdict {v!r} not in shared codomain "
            f"{SOURCE_ENTAILMENT_VERDICT_VALUES!r}"
        )
    for conf in (
        _CONF_ENTAILED_CONTAINMENT,
        _CONF_NOT_SUPPORTED_NUMERIC_MISMATCH,
        _CONF_UNCERTAIN_DEFAULT,
    ):
        assert 0.0 <= conf <= 1.0, f"mock confidence {conf!r} out of [0, 1]"


_validate_codomain_membership()


# ---------------------------------------------------------------------------
# JSON serialization (mirrors source_quality_evaluator._payload_default)
# ---------------------------------------------------------------------------
def _payload_default(o: Any) -> Any:
    """JSON encoder fallback for non-primitive payload values."""
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if isinstance(o, datetime.datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=datetime.timezone.utc)
        return o.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(o, datetime.date):
        return o.isoformat()
    raise TypeError(
        f"Object of type {type(o).__name__} is not JSON serializable "
        f"in claim_entailment_checker payloads"
    )


def _serialize_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=_payload_default,
    )


# ---------------------------------------------------------------------------
# Mock heuristic
# ---------------------------------------------------------------------------
def _normalize_text(s: str) -> str:
    """Normalize a text for containment comparison: lowercase + collapsed
    whitespace.
    """
    if s is None:
        return ""
    return " ".join(s.lower().split())


def _extract_numbers(s: str) -> list[str]:
    """Return the numeric tokens found in ``s`` as a list of strings.

    We compare numbers as strings (not as floats) so '37' and '37.0'
    are not collapsed: in a factual claim the spelling of a number is
    semantically meaningful (3 vs 3.0 vs 30%).
    """
    if s is None:
        return []
    return _NUMBER_RE.findall(s)


def _apply_mock_heuristic(
    *, claim_text: str, quote_text: str
) -> tuple[str, float, str, dict[str, Any]]:
    """Apply the three deterministic rules and return
    (verdict, confidence, rationale, heuristic_payload).

    The heuristic_payload is a small dict embedded in the row payload
    so downstream consumers can inspect which rule fired and on what
    evidence. It does NOT contain stack traces, secrets, or any data
    other than what was already on the row inputs.
    """
    claim_norm = _normalize_text(claim_text)
    quote_norm = _normalize_text(quote_text)
    claim_numbers = _extract_numbers(claim_text)
    quote_numbers = _extract_numbers(quote_text)

    heuristic_payload: dict[str, Any] = {
        "numbers": {
            "claim": claim_numbers,
            "quote": quote_numbers,
        },
    }

    # Rule 1 — containment match.
    if claim_norm and quote_norm and (
        claim_norm == quote_norm
        or claim_norm in quote_norm
        or quote_norm in claim_norm
    ):
        heuristic_payload["heuristic"] = "containment_match"
        return (
            VERDICT_ENTAILED,
            _CONF_ENTAILED_CONTAINMENT,
            "mock containment match",
            heuristic_payload,
        )

    # Rule 2 — numeric mismatch.
    # Both texts have numbers AND the sets of numbers differ.
    if claim_numbers and quote_numbers and (
        set(claim_numbers) != set(quote_numbers)
    ):
        heuristic_payload["heuristic"] = "numeric_mismatch"
        return (
            VERDICT_NOT_SUPPORTED,
            _CONF_NOT_SUPPORTED_NUMERIC_MISMATCH,
            "mock numeric mismatch",
            heuristic_payload,
        )

    # Default — uncertain.
    heuristic_payload["heuristic"] = "default_uncertain"
    return (
        VERDICT_UNCERTAIN,
        _CONF_UNCERTAIN_DEFAULT,
        "mock heuristic could not establish entailment",
        heuristic_payload,
    )


# ---------------------------------------------------------------------------
# Target lookup helpers
# ---------------------------------------------------------------------------
def _lock_and_load_claim_scope(
    conn: Connection, *, claim_ledger_entry_id: uuid.UUID
) -> dict[str, Any] | None:
    """Lock the claim_ledger_entries row and return the canonical scope.

    Joins claim_ledger_entries -> logical_claims to read the tenant /
    project / task / canonical_claim_text. The lock is on the entry
    row only; the upstream logical_claims row is read but not locked
    (it cannot be mutated independently in the context of an
    in-progress task).

    Returns None when the entry does not exist.
    """
    row = conn.execute(
        text(
            """
            SELECT
              cle.id                       AS claim_ledger_entry_id,
              cle.claim_logical_id         AS claim_logical_id,
              lc.tenant_id                 AS tenant_id,
              lc.project_id                AS project_id,
              lc.task_id                   AS task_id,
              lc.canonical_claim_text      AS canonical_claim_text
            FROM claim_ledger_entries cle
            JOIN logical_claims       lc ON lc.id = cle.claim_logical_id
            WHERE cle.id = :entry_id
            FOR UPDATE OF cle
            """
        ),
        {"entry_id": claim_ledger_entry_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _load_evidence_span(
    conn: Connection, *, evidence_span_id: uuid.UUID
) -> dict[str, Any] | None:
    """Load the evidence_span row (the quote text). Returns None if
    not found.

    No lock is needed here: evidence_spans is append-only at DB level
    (trigger evidence_spans_append_only), so the row cannot mutate
    out from under us.
    """
    row = conn.execute(
        text(
            """
            SELECT
              es.id    AS evidence_span_id,
              es.quote AS quote
            FROM evidence_spans es
            WHERE es.id = :es_id
            """
        ),
        {"es_id": evidence_span_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# Idempotency / version lookup
# ---------------------------------------------------------------------------
def _select_existing_by_idempotency(
    conn: Connection,
    *,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    idempotency_key: str,
) -> dict[str, Any] | None:
    """Return the existing row for (entry, span, idempotency_key), or
    None.

    The query uses the UNIQUE index ``cec_entry_span_idem_uq`` for an
    O(log n) lookup.
    """
    row = conn.execute(
        text(
            """
            SELECT id, version_no, verdict
            FROM claim_entailment_checks
            WHERE claim_ledger_entry_id = :entry
              AND evidence_span_id      = :span
              AND idempotency_key       = :idem
            """
        ),
        {
            "entry": claim_ledger_entry_id,
            "span": evidence_span_id,
            "idem": idempotency_key,
        },
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "id": uuid.UUID(str(m["id"])),
        "version_no": int(m["version_no"]),
        "verdict": str(m["verdict"]),
    }


def _v1_exists_for_pair(
    conn: Connection,
    *,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> bool:
    """Return True iff there is already a v1 row for the (entry, span)
    pair under SOME idempotency_key.

    Caller MUST have acquired the parent lock on claim_ledger_entries
    before invoking this; otherwise the read is racy with respect to
    a concurrent inserter.
    """
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM claim_entailment_checks
            WHERE claim_ledger_entry_id = :entry
              AND evidence_span_id      = :span
              AND version_no            = 1
            LIMIT 1
            """
        ),
        {"entry": claim_ledger_entry_id, "span": evidence_span_id},
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# INSERT helper
# ---------------------------------------------------------------------------
def _insert_check_row(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    version_no: int,
    verdict: str,
    confidence: float | None,
    checker_name: str,
    checker_version: str,
    policy_name: str,
    policy_version: str,
    idempotency_key: str,
    rationale: str | None,
    payload: dict[str, Any],
) -> uuid.UUID:
    """INSERT a new claim_entailment_checks row and return its id.

    The id column is intentionally omitted from the INSERT so the
    table-level DEFAULT app_new_uuid() applies. The CHECK constraints
    and FK on (entry_id, claim_logical_id) are enforced by the DB.
    """
    row = conn.execute(
        text(
            """
            INSERT INTO claim_entailment_checks (
                tenant_id, project_id, task_id,
                claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                version_no, verdict, confidence,
                checker_name, checker_version,
                policy_name, policy_version,
                idempotency_key, rationale, payload
            ) VALUES (
                :tenant_id, :project_id, :task_id,
                :claim_logical_id, :claim_ledger_entry_id, :evidence_span_id,
                :version_no, :verdict, :confidence,
                :checker_name, :checker_version,
                :policy_name, :policy_version,
                :idempotency_key, :rationale, CAST(:payload AS JSONB)
            )
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "task_id": task_id,
            "claim_logical_id": claim_logical_id,
            "claim_ledger_entry_id": claim_ledger_entry_id,
            "evidence_span_id": evidence_span_id,
            "version_no": version_no,
            "verdict": verdict,
            "confidence": confidence,
            "checker_name": checker_name,
            "checker_version": checker_version,
            "policy_name": policy_name,
            "policy_version": policy_version,
            "idempotency_key": idempotency_key,
            "rationale": rationale,
            "payload": _serialize_payload(payload),
        },
    ).first()
    return uuid.UUID(str(row[0]))


# ---------------------------------------------------------------------------
# Result builder
# ---------------------------------------------------------------------------
def _empty_result(status: str, *, error_code: str | None = None) -> dict[str, Any]:
    """Return a fully-populated result dict with ``status`` set as given.

    Shape stable across every return path so the caller can read every
    key without conditional checks.
    """
    return {
        "status": status,
        "assessment_id": None,
        "version_no": None,
        "verdict": None,
        "claim_ledger_entry_id": None,
        "evidence_span_id": None,
        "tenant_id": None,
        "project_id": None,
        "task_id": None,
        "error_code": error_code,
    }


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def assess_claim_entailment(
    conn: Connection,
    *,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    idempotency_key: str,
    checker_name: str = DEFAULT_CHECKER_NAME,
    checker_version: str = DEFAULT_CHECKER_VERSION,
    policy_name: str = DEFAULT_POLICY_NAME,
    policy_version: str = DEFAULT_POLICY_VERSION,
) -> dict[str, Any]:
    """Append a mock claim_entailment_checks row for the (entry, span)
    pair.

    Steps:
      1. Validate inputs. The caller MUST supply both
         ``claim_ledger_entry_id`` and ``evidence_span_id`` as
         well-formed UUIDs; otherwise return
         ``status='invalid_target'`` without any DB write.
      2. Lock the parent claim_ledger_entries row (FOR UPDATE) and
         read its canonical scope (tenant_id, project_id, task_id,
         claim_logical_id, canonical_claim_text). If the entry does
         not exist -> return ``status='not_found'``.
      3. Load the evidence_span row (quote text). If the span does
         not exist -> return ``status='not_found'``.
      4. Check for an existing row with the SAME idempotency_key on
         the SAME (entry, span) pair. If present -> return
         ``status='already_assessed'`` with the existing id, version
         and verdict; NO new row is inserted.
      5. If a v1 row already exists for the pair under a DIFFERENT
         idempotency_key, return ``status='error'`` with
         ``error_code='entailment_version_conflict'`` (MVP-0 fixes
         version_no=1 and refuses to silently mask the collision).
      6. Apply the mock heuristic to (canonical_claim_text, quote)
         and INSERT a new row inside a SAVEPOINT.
      7. On a UNIQUE-violation race against a concurrent inserter
         (cec_entry_span_idem_uq), re-read by idempotency_key and
         return ``status='already_assessed'``. On a UNIQUE-violation
         caused by ``cec_entry_span_version_uq``, return
         ``status='error'`` with
         ``error_code='entailment_version_conflict'``.

    Side effects:
      Writes EXACTLY one row to claim_entailment_checks on success.
      Reads (with FOR UPDATE on claim_ledger_entries) the entry row.
      Does NOT touch any other table.

    Args:
      conn:
        SQLAlchemy Connection inside an active transaction. Must NOT
        commit or rollback on behalf of the caller.
      claim_ledger_entry_id:
        UUID of the claim_ledger_entries row being evaluated. The
        function locks this row FOR UPDATE.
      evidence_span_id:
        UUID of the evidence_spans row that allegedly supports the
        claim.
      idempotency_key:
        Caller-supplied opaque token. A redelivery with the same key
        on the same (entry, span) collapses to
        ``status='already_assessed'``.
      checker_name, checker_version, policy_name, policy_version:
        Provenance fields. Defaults are the MVP-0 mock checker
        identity (see module identity above).
    """
    # Step 1: input validation. We treat None or non-UUID inputs as
    # invalid_target so the function remains usable from both code
    # paths and tests without try/except gymnastics. The defensive
    # checks below guard against direct caller mistakes; the type
    # annotations document the intended contract.
    if claim_ledger_entry_id is None or evidence_span_id is None:
        logger.info(
            "claim_entailment_checker.invalid_target",
            claim_ledger_entry_id_set=claim_ledger_entry_id is not None,
            evidence_span_id_set=evidence_span_id is not None,
        )
        return _empty_result(STATUS_INVALID_TARGET)
    if not isinstance(claim_ledger_entry_id, uuid.UUID) or not isinstance(
        evidence_span_id, uuid.UUID
    ):
        logger.info(
            "claim_entailment_checker.invalid_target_type",
            claim_ledger_entry_id_type=type(claim_ledger_entry_id).__name__,
            evidence_span_id_type=type(evidence_span_id).__name__,
        )
        return _empty_result(STATUS_INVALID_TARGET)
    if not idempotency_key or not isinstance(idempotency_key, str):
        logger.info(
            "claim_entailment_checker.invalid_target_idempotency_key",
        )
        return _empty_result(STATUS_INVALID_TARGET)

    # Step 2: lock the parent entry row and read canonical scope.
    claim_scope = _lock_and_load_claim_scope(
        conn, claim_ledger_entry_id=claim_ledger_entry_id
    )
    if claim_scope is None:
        logger.info(
            "claim_entailment_checker.not_found_claim_ledger_entry",
            claim_ledger_entry_id=str(claim_ledger_entry_id),
        )
        return _empty_result(STATUS_NOT_FOUND)

    tenant_id = uuid.UUID(str(claim_scope["tenant_id"]))
    project_id = (
        uuid.UUID(str(claim_scope["project_id"]))
        if claim_scope.get("project_id") is not None
        else None
    )
    task_id = uuid.UUID(str(claim_scope["task_id"]))
    claim_logical_id = uuid.UUID(str(claim_scope["claim_logical_id"]))
    canonical_claim_text = str(claim_scope.get("canonical_claim_text") or "")

    # Step 3: load the evidence_span row.
    span_row = _load_evidence_span(conn, evidence_span_id=evidence_span_id)
    if span_row is None:
        logger.info(
            "claim_entailment_checker.not_found_evidence_span",
            evidence_span_id=str(evidence_span_id),
        )
        return _empty_result(STATUS_NOT_FOUND)

    quote_text = str(span_row.get("quote") or "")

    # Step 4: idempotency short-circuit.
    existing = _select_existing_by_idempotency(
        conn,
        claim_ledger_entry_id=claim_ledger_entry_id,
        evidence_span_id=evidence_span_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        logger.info(
            "claim_entailment_checker.already_assessed",
            claim_ledger_entry_id=str(claim_ledger_entry_id),
            evidence_span_id=str(evidence_span_id),
            assessment_id=str(existing["id"]),
            version_no=existing["version_no"],
        )
        return {
            "status": STATUS_ALREADY_ASSESSED,
            "assessment_id": str(existing["id"]),
            "version_no": existing["version_no"],
            "verdict": existing["verdict"],
            "claim_ledger_entry_id": str(claim_ledger_entry_id),
            "evidence_span_id": str(evidence_span_id),
            "tenant_id": str(tenant_id),
            "project_id": str(project_id) if project_id is not None else None,
            "task_id": str(task_id),
            "error_code": None,
        }

    # Step 5: in MVP-0, version_no is fixed at 1. A pre-existing v1
    # under a different idempotency_key is a programming-error
    # condition (the orchestrator must use the deterministic key
    # documented in PHASE_8_8A_PRE.md §7.2). We surface it as
    # status='error' rather than silently masking — exact wording of
    # block prompt §6 ("se collisione su version unique ma
    # idempotency diverso, gestisci come error o invalid_target in
    # modo esplicito. Non mascherare.").
    if _v1_exists_for_pair(
        conn,
        claim_ledger_entry_id=claim_ledger_entry_id,
        evidence_span_id=evidence_span_id,
    ):
        logger.warning(
            "claim_entailment_checker.entailment_version_conflict",
            claim_ledger_entry_id=str(claim_ledger_entry_id),
            evidence_span_id=str(evidence_span_id),
            idempotency_key=idempotency_key,
        )
        return {
            "status": STATUS_ERROR,
            "assessment_id": None,
            "version_no": None,
            "verdict": None,
            "claim_ledger_entry_id": str(claim_ledger_entry_id),
            "evidence_span_id": str(evidence_span_id),
            "tenant_id": str(tenant_id),
            "project_id": str(project_id) if project_id is not None else None,
            "task_id": str(task_id),
            "error_code": "entailment_version_conflict",
        }

    # Step 6: apply the mock heuristic.
    verdict, confidence, rationale, heuristic_payload = _apply_mock_heuristic(
        claim_text=canonical_claim_text,
        quote_text=quote_text,
    )

    # Build the JSONB payload. The shape is documented at the top of
    # the module: ``mock``, ``semantic_warning``, ``input``,
    # ``heuristic``, ``numbers``. No stack traces, no secrets.
    payload: dict[str, Any] = {
        "mock": True,
        "semantic_warning": _SEMANTIC_WARNING,
        "input": {
            "claim_text": canonical_claim_text,
            "quote": quote_text,
        },
        "heuristic": heuristic_payload.get("heuristic"),
        "numbers": heuristic_payload.get("numbers", {}),
        "service_name": SERVICE_NAME,
        "service_version": SERVICE_VERSION,
    }

    # Step 7: INSERT inside a SAVEPOINT.
    try:
        with conn.begin_nested():
            new_id = _insert_check_row(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                claim_logical_id=claim_logical_id,
                claim_ledger_entry_id=claim_ledger_entry_id,
                evidence_span_id=evidence_span_id,
                version_no=1,
                verdict=verdict,
                confidence=confidence,
                checker_name=checker_name,
                checker_version=checker_version,
                policy_name=policy_name,
                policy_version=policy_version,
                idempotency_key=idempotency_key,
                rationale=rationale,
                payload=payload,
            )
    except IntegrityError as exc:
        # Two possible UNIQUE-violation paths:
        #   (a) cec_entry_span_idem_uq — a concurrent inserter won
        #       the race for the same (entry, span, idempotency_key).
        #       Resolve by re-reading and returning
        #       status='already_assessed'.
        #   (b) cec_entry_span_version_uq — another writer inserted
        #       a v1 for the same (entry, span) pair with a different
        #       idempotency_key in between our _v1_exists_for_pair
        #       check and our INSERT. Surface as
        #       status='error' / 'entailment_version_conflict'.
        # We disambiguate by attempting the idempotency-keyed lookup
        # first; if that finds a row, it was case (a), otherwise case
        # (b) (or some unexpected DB error, which we re-raise to make
        # the regression visible).
        existing_after_race = _select_existing_by_idempotency(
            conn,
            claim_ledger_entry_id=claim_ledger_entry_id,
            evidence_span_id=evidence_span_id,
            idempotency_key=idempotency_key,
        )
        if existing_after_race is not None:
            logger.info(
                "claim_entailment_checker.race_resolved_to_already_assessed",
                claim_ledger_entry_id=str(claim_ledger_entry_id),
                evidence_span_id=str(evidence_span_id),
                assessment_id=str(existing_after_race["id"]),
                version_no=existing_after_race["version_no"],
                integrity_error=str(exc),
            )
            return {
                "status": STATUS_ALREADY_ASSESSED,
                "assessment_id": str(existing_after_race["id"]),
                "version_no": existing_after_race["version_no"],
                "verdict": existing_after_race["verdict"],
                "claim_ledger_entry_id": str(claim_ledger_entry_id),
                "evidence_span_id": str(evidence_span_id),
                "tenant_id": str(tenant_id),
                "project_id": (
                    str(project_id) if project_id is not None else None
                ),
                "task_id": str(task_id),
                "error_code": None,
            }
        # Case (b): version_no UNIQUE collided. Check whether a v1
        # actually now exists for this pair: if yes, this is
        # entailment_version_conflict; if no, we have an unexpected
        # IntegrityError and re-raise so the caller / tests see it.
        if _v1_exists_for_pair(
            conn,
            claim_ledger_entry_id=claim_ledger_entry_id,
            evidence_span_id=evidence_span_id,
        ):
            logger.warning(
                "claim_entailment_checker.race_resolved_to_version_conflict",
                claim_ledger_entry_id=str(claim_ledger_entry_id),
                evidence_span_id=str(evidence_span_id),
                idempotency_key=idempotency_key,
                integrity_error=str(exc),
            )
            return {
                "status": STATUS_ERROR,
                "assessment_id": None,
                "version_no": None,
                "verdict": None,
                "claim_ledger_entry_id": str(claim_ledger_entry_id),
                "evidence_span_id": str(evidence_span_id),
                "tenant_id": str(tenant_id),
                "project_id": (
                    str(project_id) if project_id is not None else None
                ),
                "task_id": str(task_id),
                "error_code": "entailment_version_conflict",
            }
        # Unexpected IntegrityError shape: re-raise so the test suite
        # surfaces it visibly rather than masking it.
        raise

    logger.info(
        "claim_entailment_checker.assessed",
        claim_ledger_entry_id=str(claim_ledger_entry_id),
        evidence_span_id=str(evidence_span_id),
        assessment_id=str(new_id),
        verdict=verdict,
        version_no=1,
    )

    return {
        "status": STATUS_ASSESSED,
        "assessment_id": str(new_id),
        "version_no": 1,
        "verdict": verdict,
        "claim_ledger_entry_id": str(claim_ledger_entry_id),
        "evidence_span_id": str(evidence_span_id),
        "tenant_id": str(tenant_id),
        "project_id": str(project_id) if project_id is not None else None,
        "task_id": str(task_id),
        "error_code": None,
    }
