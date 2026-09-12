-- 0014_builtin_client_access_profiles.sql
--
-- GENERATED FILE - DO NOT EDIT BY HAND.
-- Source of truth: apps/api/src/api/authz/profiles.py (BUILTIN_PROFILES)
-- Regenerate with: make generate-role-catalogue
--
-- IAM-101's three built-in client access profiles. Generated from the same
-- definition the service and the test fake read, so the profiles a firm can
-- select cannot differ from the ones the code describes.
--
-- Each ships as version 1. A built-in is never edited (a firm composes its
-- own instead - see ClientAccessProfileService.publish_version), so there is
-- no version 2 to generate.

begin;

insert into client_access_profile (name, description, is_builtin) values
    ('Capture only', 'Upload documents, submit expenses, view own submissions.', true),
    ('Invoice and capture', 'Adds sales invoicing, customers, and viewing own reports.', true),
    ('Full self-service', 'Adds coding, bank reconciliation and VAT preparation, while the firm retains filing and period control.', true);

insert into client_access_profile_version (profile_id, version, summary, restrictions)
select p.id, 1, 'Upload documents, submit expenses, view own submissions.', '{"bank_detail_visible": false, "periods_editable": false, "reports_visible": false}'::jsonb
from client_access_profile p where p.is_builtin and p.name = 'Capture only';

insert into client_access_profile_version (profile_id, version, summary, restrictions)
select p.id, 1, 'Adds sales invoicing, customers, and viewing own reports.', '{"bank_detail_visible": false, "periods_editable": false}'::jsonb
from client_access_profile p where p.is_builtin and p.name = 'Invoice and capture';

insert into client_access_profile_version (profile_id, version, summary, restrictions)
select p.id, 1, 'Adds coding, bank reconciliation and VAT preparation, while the firm retains filing and period control.', '{"periods_editable": false}'::jsonb
from client_access_profile p where p.is_builtin and p.name = 'Full self-service';

insert into client_access_profile_permission (profile_version_id, permission_id)
select v.id, perm.id
from client_access_profile_version v
join client_access_profile p on p.id = v.profile_id
join (values
    ('Capture only', 'submit', 'expense'),
    ('Capture only', 'upload', 'document'),
    ('Capture only', 'view', 'administration'),
    ('Capture only', 'view', 'document'),
    ('Invoice and capture', 'create', 'sales_invoice'),
    ('Invoice and capture', 'manage', 'customer'),
    ('Invoice and capture', 'send', 'sales_invoice'),
    ('Invoice and capture', 'submit', 'expense'),
    ('Invoice and capture', 'upload', 'document'),
    ('Invoice and capture', 'view', 'administration'),
    ('Invoice and capture', 'view', 'chart_of_accounts'),
    ('Invoice and capture', 'view', 'document'),
    ('Invoice and capture', 'view', 'report'),
    ('Full self-service', 'code', 'purchase_invoice'),
    ('Full self-service', 'create', 'sales_invoice'),
    ('Full self-service', 'export', 'report_data'),
    ('Full self-service', 'manage', 'customer'),
    ('Full self-service', 'prepare', 'vat_return'),
    ('Full self-service', 'read', 'audit_log'),
    ('Full self-service', 'reconcile', 'bank_transaction'),
    ('Full self-service', 'send', 'sales_invoice'),
    ('Full self-service', 'submit', 'expense'),
    ('Full self-service', 'upload', 'document'),
    ('Full self-service', 'view', 'administration'),
    ('Full self-service', 'view', 'bank_transaction'),
    ('Full self-service', 'view', 'chart_of_accounts'),
    ('Full self-service', 'view', 'document'),
    ('Full self-service', 'view', 'purchase_invoice'),
    ('Full self-service', 'view', 'report')
) as m(profile_name, action, resource_type) on m.profile_name = p.name
join permission perm on perm.action = m.action and perm.resource_type = m.resource_type
where p.is_builtin and v.version = 1;

commit;
