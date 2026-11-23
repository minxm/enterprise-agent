-- Tighten business_app access to the Flyway history table.
--
-- Executor: business_migrator, the table owner.
-- Do not run this as business_app. Do not use a PostgreSQL superuser as the
-- application login to work around it. This file is not a Flyway version and
-- must stay outside backend-java/src/main/resources/db/migration.
--
-- Default privileges grant business_app SELECT, INSERT, UPDATE and DELETE on
-- tables created by business_migrator. Flyway creates its history table under
-- that role, so the grant lands on business.flyway_schema_history as well.
-- Ordinary business tables keep the default DML grant. This script removes
-- only the history-table write privileges and leaves SELECT in place.
-- The Java migrate task and the dev Flyway startup run the same three
-- statements after Flyway, still connected as business_migrator.

DO $$
BEGIN
    IF current_user <> 'business_migrator' THEN
        RAISE EXCEPTION 'must be executed by business_migrator, current_user=%', current_user;
    END IF;
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'business_migrator must not be a superuser';
    END IF;
END
$$;

REVOKE ALL ON TABLE business.flyway_schema_history FROM business_app;
REVOKE ALL ON TABLE business.flyway_schema_history FROM PUBLIC;
GRANT SELECT ON TABLE business.flyway_schema_history TO business_app;

DO $$
BEGIN
    IF NOT has_table_privilege('business_app', 'business.flyway_schema_history', 'SELECT')
        OR has_table_privilege('business_app', 'business.flyway_schema_history', 'INSERT')
        OR has_table_privilege('business_app', 'business.flyway_schema_history', 'UPDATE')
        OR has_table_privilege('business_app', 'business.flyway_schema_history', 'DELETE')
        OR has_table_privilege('business_app', 'business.flyway_schema_history', 'TRUNCATE') THEN
        RAISE EXCEPTION 'business_app flyway history privileges are not read-only';
    END IF;
END
$$;
