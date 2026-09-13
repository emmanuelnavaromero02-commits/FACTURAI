-- ==============================================================================
-- Facturia - Database Initialization Script
-- Implements Rule 2: Two distinct database roles:
-- 1. facturia_owner: Table owner, runs migrations (Alembic)
-- 2. facturia_app: Non-owner application role for FastAPI & Workers
--    Row-Level Security (RLS) is strictly enforced on non-owners.
-- ==============================================================================

-- 1. Create Roles
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'facturia_owner') THEN
        CREATE ROLE facturia_owner WITH LOGIN PASSWORD 'facturia_owner_secret';
    END IF;

    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'facturia_app') THEN
        CREATE ROLE facturia_app WITH LOGIN PASSWORD 'facturia_app_secret' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

-- 2. Configure Database and Schema Ownership
ALTER DATABASE facturia OWNER TO facturia_owner;
ALTER SCHEMA public OWNER TO facturia_owner;

-- 3. Grants for owner
GRANT ALL ON SCHEMA public TO facturia_owner;

-- 4. Grants for application runtime (facturia_app)
GRANT CONNECT ON DATABASE facturia TO facturia_app;
GRANT USAGE ON SCHEMA public TO facturia_app;

-- 5. Default Privileges: Any future table or sequence created by facturia_owner
-- will be accessible to facturia_app for standard DML operations.
ALTER DEFAULT PRIVILEGES FOR ROLE facturia_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO facturia_app;

ALTER DEFAULT PRIVILEGES FOR ROLE facturia_owner IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO facturia_app;

-- 6. Apply to any existing objects in public schema
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO facturia_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO facturia_app;
