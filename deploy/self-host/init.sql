\set ON_ERROR_STOP on
\set ECHO none
SET log_min_error_statement = PANIC;
SET log_error_verbosity = TERSE;
-- Only the database container sees this password. It is never a psql argument.
SELECT format('CREATE ROLE knowledge_layer LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 24',
              btrim(pg_read_file('/run/secrets/application_password')))
\gexec
CREATE DATABASE knowledge_layer OWNER knowledge_layer;
REVOKE ALL ON DATABASE knowledge_layer FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE knowledge_layer TO knowledge_layer;
\connect knowledge_layer
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO knowledge_layer;
