-- ============================================================================
-- 0002_storage.sql
-- Evidence-First MVP-0 — Sprint 1 — Storage layer.
--
-- Contenuto:
--   - storage_blobs (deduplicato, refcount-based)
--   - storage_objects (logical owners)
--   - trigger refcount inc/dec
--   - trigger enforce_blob_tenant_scope
--   - trigger reject_delete_blob_with_refs
--
-- Dipendenze: 0001_foundation.sql (tenants, projects, app_new_uuid).
--
-- Note:
--   - DEDUP_SCOPE: in MVP-0 tutti i blob sono globali (tenant_namespace_id IS NULL).
--   - refcount aggiornato dai trigger sui figli storage_objects, mai a mano.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- STORAGE_BLOBS
-- ---------------------------------------------------------------------------
CREATE TABLE storage_blobs (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_namespace_id UUID                 REFERENCES tenants(id) ON DELETE RESTRICT,
  content_hash        TEXT        NOT NULL,
  hash_algorithm      TEXT        NOT NULL DEFAULT 'sha256'
                                  CHECK (hash_algorithm IN ('sha256')),
  size_bytes          BIGINT      NOT NULL CHECK (size_bytes >= 0),
  mime_type           TEXT,
  storage_backend     TEXT        NOT NULL
                                  CHECK (storage_backend IN ('local_fs','s3','gcs','azure_blob')),
  local_path          TEXT,
  bucket              TEXT,
  object_key          TEXT,
  refcount            BIGINT      NOT NULL DEFAULT 0 CHECK (refcount >= 0),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT blob_location_present CHECK (
    (storage_backend = 'local_fs'
       AND local_path IS NOT NULL
       AND bucket IS NULL
       AND object_key IS NULL)
    OR
    (storage_backend IN ('s3','gcs','azure_blob')
       AND bucket IS NOT NULL
       AND object_key IS NOT NULL
       AND local_path IS NULL)
  )
);

-- Dedup global (DEDUP_SCOPE=global): tenant_namespace_id IS NULL.
CREATE UNIQUE INDEX sb_global_uq
  ON storage_blobs (content_hash, hash_algorithm)
  WHERE tenant_namespace_id IS NULL;

-- Dedup per tenant (riservato per evoluzioni future, non usato in MVP-0).
CREATE UNIQUE INDEX sb_tenant_uq
  ON storage_blobs (tenant_namespace_id, content_hash, hash_algorithm)
  WHERE tenant_namespace_id IS NOT NULL;

CREATE INDEX storage_blobs_hash_idx ON storage_blobs (content_hash);

-- ---------------------------------------------------------------------------
-- STORAGE_OBJECTS
-- ---------------------------------------------------------------------------
CREATE TABLE storage_objects (
  id                 UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id          UUID        NOT NULL REFERENCES tenants(id)  ON DELETE RESTRICT,
  project_id         UUID                 REFERENCES projects(id) ON DELETE RESTRICT,
  blob_id            UUID        NOT NULL REFERENCES storage_blobs(id) ON DELETE RESTRICT,
  object_type        TEXT        NOT NULL,
  logical_owner_kind TEXT        NOT NULL,
  logical_owner_id   UUID        NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT so_owner_uq UNIQUE
    (tenant_id, project_id, object_type, logical_owner_kind, logical_owner_id, blob_id)
);

CREATE INDEX storage_objects_blob_idx   ON storage_objects (blob_id);
CREATE INDEX storage_objects_owner_idx
  ON storage_objects (logical_owner_kind, logical_owner_id);
CREATE INDEX storage_objects_tenant_idx ON storage_objects (tenant_id);

-- ---------------------------------------------------------------------------
-- Trigger: refcount inc/dec
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION storage_object_refcount_ins() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE storage_blobs
  SET refcount = refcount + 1
  WHERE id = NEW.blob_id;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION storage_object_refcount_del() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE storage_blobs
  SET refcount = refcount - 1
  WHERE id = OLD.blob_id;
  RETURN OLD;
END;
$$;

CREATE TRIGGER storage_object_refcount_ins_trg
AFTER INSERT ON storage_objects
FOR EACH ROW EXECUTE FUNCTION storage_object_refcount_ins();

CREATE TRIGGER storage_object_refcount_del_trg
AFTER DELETE ON storage_objects
FOR EACH ROW EXECUTE FUNCTION storage_object_refcount_del();

-- ---------------------------------------------------------------------------
-- Trigger: enforce blob tenant scope
-- Se il blob ha tenant_namespace_id NOT NULL, l'oggetto che lo riferisce deve
-- avere lo stesso tenant_id. In MVP-0 tutti i blob sono globali, quindi il
-- controllo è effettivamente no-op, ma il trigger esiste per quando si vorrà
-- attivare DEDUP_SCOPE=tenant.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION enforce_blob_tenant_scope() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  ns UUID;
BEGIN
  SELECT tenant_namespace_id INTO ns FROM storage_blobs WHERE id = NEW.blob_id;
  IF ns IS NOT NULL AND ns <> NEW.tenant_id THEN
    RAISE EXCEPTION 'enforce_blob_tenant_scope: blob % belongs to tenant_namespace % but storage_objects.tenant_id is %',
      NEW.blob_id, ns, NEW.tenant_id;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER enforce_blob_tenant_scope_trg
BEFORE INSERT OR UPDATE ON storage_objects
FOR EACH ROW EXECUTE FUNCTION enforce_blob_tenant_scope();

-- ---------------------------------------------------------------------------
-- Trigger: reject DELETE storage_blobs when refcount > 0
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION reject_delete_blob_with_refs() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.refcount > 0 THEN
    RAISE EXCEPTION 'reject_delete_blob_with_refs: blob % has refcount=%; cannot DELETE',
      OLD.id, OLD.refcount;
  END IF;
  RETURN OLD;
END;
$$;

CREATE TRIGGER reject_delete_blob_with_refs_trg
BEFORE DELETE ON storage_blobs
FOR EACH ROW EXECUTE FUNCTION reject_delete_blob_with_refs();

-- ============================================================================
-- FINE 0002_storage.sql
-- ============================================================================