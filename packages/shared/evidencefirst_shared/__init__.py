"""Evidence-First shared package: canonical JSON, error model, schemas, db helpers."""

from .canonical_json import canonical_dumps, canonical_sha256  # noqa: F401
from .errors import NormalizedError, ErrorCode  # noqa: F401

__all__ = [
    "canonical_dumps",
    "canonical_sha256",
    "NormalizedError",
    "ErrorCode",
]