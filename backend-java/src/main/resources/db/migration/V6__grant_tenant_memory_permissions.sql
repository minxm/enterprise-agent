-- Tenant memory permissions. Existing history stays unchanged.
--
-- role and role_permission use FORCE ROW LEVEL SECURITY, so the migrator
-- cannot see other tenants' rows while the policies are on. They are
-- disabled only for this grant, then forced back on in the same migration.
-- The grant targets tenant_admin alone.

INSERT INTO business.permission (id, code, name)
SELECT gen_random_uuid(), 'memory:read_tenant', '读取租户记忆'
WHERE NOT EXISTS (
    SELECT 1 FROM business.permission WHERE code = 'memory:read_tenant'
);

INSERT INTO business.permission (id, code, name)
SELECT gen_random_uuid(), 'memory:manage_tenant', '管理租户记忆'
WHERE NOT EXISTS (
    SELECT 1 FROM business.permission WHERE code = 'memory:manage_tenant'
);

ALTER TABLE business.role DISABLE ROW LEVEL SECURITY;
ALTER TABLE business.role_permission DISABLE ROW LEVEL SECURITY;

INSERT INTO business.role_permission (role_id, permission_id, tenant_id)
SELECT role.id, permission.id, role.tenant_id
FROM business.role role
JOIN business.permission permission
  ON permission.code IN ('memory:read_tenant', 'memory:manage_tenant')
WHERE role.code = 'tenant_admin'
ON CONFLICT ON CONSTRAINT role_permission_pkey DO NOTHING;

ALTER TABLE business.role ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.role FORCE ROW LEVEL SECURITY;
ALTER TABLE business.role_permission ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.role_permission FORCE ROW LEVEL SECURITY;
