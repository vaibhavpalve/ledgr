-- 0066_permissions_added_to_0010_after_apply.sql
--
-- 0010_role_catalogue.sql is generated from api.authz.matrix. It was regenerated
-- (fixed_asset view/manage, sales_invoice approve) AFTER production had already
-- applied it, so production never received those rows and migrate.py refused to
-- run because 0010's checksum no longer matched.
--
-- This carries exactly those rows as a forward migration. Every insert is
-- idempotent (permission_unique_triple, role_permission's primary key), so on a
-- database built from the current 0010 it changes nothing. It touches only the
-- permission catalogue - no tenant data, no ledger tables.
begin;

insert into permission (action, resource_type, resource_scope, description) values
    ('approve', 'sales_invoice', 'administration', 'Approve sales invoices'),
    ('manage', 'fixed_asset', 'administration', 'Create, depreciate and dispose fixed assets'),
    ('view', 'fixed_asset', 'administration', 'View fixed assets')
on conflict (action, resource_type, resource_scope) do nothing;

insert into role_permission (role_id, permission_id)
select r.id, p.id
from "role" r
join (values
    ('Owner', 'approve', 'sales_invoice'),
    ('Owner', 'manage', 'fixed_asset'),
    ('Owner', 'view', 'fixed_asset'),
    ('Accountant', 'manage', 'fixed_asset'),
    ('Accountant', 'view', 'fixed_asset'),
    ('Bookkeeper', 'manage', 'fixed_asset'),
    ('Bookkeeper', 'view', 'fixed_asset'),
    ('Viewer', 'view', 'fixed_asset')
) as m(role_name, action, resource_type) on m.role_name = r.name
join permission p on p.action = m.action and p.resource_type = m.resource_type
where r.is_system
on conflict (role_id, permission_id) do nothing;

commit;
