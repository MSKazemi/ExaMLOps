-- Create a dedicated database for the Prefect server's metadata store.
-- Runs only on FIRST initialization of the postgres_data volume (standard
-- postgres /docker-entrypoint-initdb.d behavior). For an already-initialized
-- volume, create it once manually:
--   docker compose exec postgres createdb -U mlops prefect
CREATE DATABASE prefect;
