-- ============================================================================
-- 0001_dev_seed.sql
-- Seed di sviluppo idempotente per Evidence-First MVP-0.
--
-- Usa ON CONFLICT DO NOTHING per consentire riesecuzioni senza duplicare righe.
-- I valori UUID sono lasciati al default (app_new_uuid) e referenziati via slug/email/name.
-- ============================================================================

-- 1) Tenant di sviluppo
INSERT INTO tenants (name, slug, status)
VALUES ('Dev Tenant', 'dev', 'active')
ON CONFLICT (slug) DO NOTHING;

-- 2) Utente di sviluppo (sotto il tenant 'dev')
INSERT INTO users (tenant_id, email, display_name, status)
SELECT t.id, 'dev@local', 'Dev User', 'active'
FROM tenants t
WHERE t.slug = 'dev'
ON CONFLICT (tenant_id, email) DO NOTHING;

-- 3) Project default
INSERT INTO projects (tenant_id, name, mode_default, created_by)
SELECT
  t.id,
  'default',
  'closed_corpus',
  u.id
FROM tenants t
JOIN users u ON u.tenant_id = t.id AND u.email = 'dev@local'
WHERE t.slug = 'dev'
ON CONFLICT (tenant_id, name) DO NOTHING;

-- 4) Project membership: l'utente dev è owner del progetto default
INSERT INTO project_members (project_id, user_id, role)
SELECT p.id, u.id, 'owner'
FROM projects p
JOIN users    u ON u.tenant_id = p.tenant_id AND u.email = 'dev@local'
JOIN tenants  t ON t.id = p.tenant_id        AND t.slug  = 'dev'
WHERE p.name = 'default'
ON CONFLICT (project_id, user_id) DO NOTHING;

-- 5) Policy version baseline MVP-0 (default per il tenant)
INSERT INTO policy_versions (tenant_id, name, is_default, metadata)
SELECT
  t.id,
  'mvp0-baseline',
  TRUE,
  jsonb_build_object(
    'description', 'MVP-0 baseline policy: MockProvider only, closed_corpus only, no external API.',
    'risk_tier_default', 'standard',
    'providers_enabled', jsonb_build_array('mock'),
    'max_cost_per_task_usd', 0
  )
FROM tenants t
WHERE t.slug = 'dev'
ON CONFLICT (tenant_id, name) DO NOTHING;