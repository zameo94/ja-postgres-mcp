-- Least-privilege role for the ja-pst MCP server.
--
-- Run ONCE as a DBA/superuser against the target database. The MCP server never
-- creates roles: it connects with the role created here, which is the real
-- security boundary (the server's READ ONLY transaction is defence in depth).
--
-- Adjust the values below, then run:
--     psql -d <dbname> -f db/roles/least_privilege.sql
--
-- The role may CONNECT, USAGE on the listed schema(s) and SELECT on their
-- tables. It has no write/DDL privilege, no membership in privileged roles and
-- no EXECUTE on dangerous functions/extensions.

\set role_name     ja_pst_readonly
\set role_password 'CHANGE_ME'
\set dbname        ja_pst
\set schema        public

-- Create the role if it does not exist (idempotent).
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role_name', :'role_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role_name')
\gexec

-- Connect and read only.
GRANT CONNECT ON DATABASE :"dbname" TO :"role_name";
GRANT USAGE ON SCHEMA :"schema" TO :"role_name";
GRANT SELECT ON ALL TABLES IN SCHEMA :"schema" TO :"role_name";
ALTER DEFAULT PRIVILEGES IN SCHEMA :"schema" GRANT SELECT ON TABLES TO :"role_name";

-- Belt and suspenders: no CREATE on the schema.
REVOKE CREATE ON SCHEMA :"schema" FROM :"role_name";

-- Do NOT grant, and make sure the role is not a member of: superuser,
-- pg_read_server_files, pg_write_server_files, pg_execute_server_program, nor
-- EXECUTE on extensions/features that can write or reach outside the database
-- (dblink, postgres_fdw writes, COPY ... PROGRAM, large-object file functions).
-- A read-only transaction does not make every SELECT safe by itself.
