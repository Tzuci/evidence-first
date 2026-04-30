"""Unit-level checks for verify_audit_chain logic that don't require a DB.

These tests verify pure helpers (_resolve_scope_id) and the canonical input
construction shape. The integration tests live in apps/api/tests and
apps/worker/tests where a real database is available.
"""
from __future__ import annotations

import uuid

import pytest

from evidencefirst_shared.db.audit import GLOBAL_SCOPE_ID, _resolve_scope_id


def test_resolve_scope_id_task_requires_task_id():
    tenant = uuid.uuid4()
    project = uuid.uuid4()
    with pytest.raises(ValueError):
        _resolve_scope_id("task", tenant_id=tenant, project_id=project, task_id=None)


def test_resolve_scope_id_project_requires_project_id():
    tenant = uuid.uuid4()
    with pytest.raises(ValueError):
        _resolve_scope_id("project", tenant_id=tenant, project_id=None, task_id=None)


def test_resolve_scope_id_task():
    tenant = uuid.uuid4()
    project = uuid.uuid4()
    task = uuid.uuid4()
    assert _resolve_scope_id("task", tenant_id=tenant, project_id=project, task_id=task) == task


def test_resolve_scope_id_tenant():
    tenant = uuid.uuid4()
    assert _resolve_scope_id("tenant", tenant_id=tenant, project_id=None, task_id=None) == tenant


def test_resolve_scope_id_global():
    assert _resolve_scope_id("global", tenant_id=uuid.uuid4(), project_id=None, task_id=None) == GLOBAL_SCOPE_ID


def test_resolve_scope_id_invalid():
    with pytest.raises(ValueError):
        _resolve_scope_id("bogus", tenant_id=uuid.uuid4(), project_id=None, task_id=None)