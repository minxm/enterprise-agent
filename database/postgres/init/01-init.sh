#!/bin/bash
# Creates the vector extension, two schemas, and four non-superuser roles.
# Passwords come from the container environment. This script does not create business tables.
set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${BUSINESS_MIGRATOR_PASSWORD:?BUSINESS_MIGRATOR_PASSWORD is required}"
: "${BUSINESS_APP_PASSWORD:?BUSINESS_APP_PASSWORD is required}"
: "${AGENT_MIGRATOR_PASSWORD:?AGENT_MIGRATOR_PASSWORD is required}"
: "${AGENT_APP_PASSWORD:?AGENT_APP_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  -v business_migrator_password="$BUSINESS_MIGRATOR_PASSWORD" \
  -v business_app_password="$BUSINESS_APP_PASSWORD" \
  -v agent_migrator_password="$AGENT_MIGRATOR_PASSWORD" \
  -v agent_app_password="$AGENT_APP_PASSWORD" \
  -v dbname="$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;

SELECT format('CREATE ROLE business_migrator LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'business_migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'business_migrator')
\gexec

SELECT format('CREATE ROLE business_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'business_app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'business_app')
\gexec

SELECT format('CREATE ROLE agent_migrator LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'agent_migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_migrator')
\gexec

SELECT format('CREATE ROLE agent_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'agent_app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app')
\gexec

CREATE SCHEMA IF NOT EXISTS business AUTHORIZATION business_migrator;
CREATE SCHEMA IF NOT EXISTS agent AUTHORIZATION agent_migrator;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT, TEMP ON DATABASE :"dbname" TO business_migrator, business_app, agent_migrator, agent_app;
GRANT USAGE ON SCHEMA public TO agent_migrator, agent_app;

GRANT USAGE ON SCHEMA business TO business_app;
GRANT USAGE ON SCHEMA agent TO agent_app;

ALTER ROLE business_migrator SET search_path = business;
ALTER ROLE business_app SET search_path = business;
ALTER ROLE agent_migrator SET search_path = agent;
ALTER ROLE agent_app SET search_path = agent;

ALTER DEFAULT PRIVILEGES FOR ROLE business_migrator IN SCHEMA business
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO business_app;
ALTER DEFAULT PRIVILEGES FOR ROLE business_migrator IN SCHEMA business
    GRANT USAGE, SELECT ON SEQUENCES TO business_app;

ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA agent
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA agent
    GRANT USAGE, SELECT ON SEQUENCES TO agent_app;
SQL
