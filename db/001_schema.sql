-- Run as the admin user against the `antiseo` database:
--   psql "host=$PGHOST dbname=antiseo user=pgadmin sslmode=require" \
--        -v ON_ERROR_STOP=1 -v crawler_pw="$CRAWLER_PW" -v api_pw="$API_PW" -f db/001_schema.sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE domains (
  id            serial PRIMARY KEY,
  host          text UNIQUE NOT NULL,
  source        text,
  status        text NOT NULL DEFAULT 'active',
  feed_url      text,
  feed_etag     text,
  feed_last_mod text,
  last_crawled  timestamptz
);

CREATE TABLE documents (
  id            bigserial PRIMARY KEY,
  domain_id     int REFERENCES domains(id),
  url           text UNIQUE NOT NULL,
  title         text NOT NULL,
  published_at  timestamptz,
  summary       text,
  body          text,
  content_hash  bytea NOT NULL,
  quality_score real,
  tsv           tsvector GENERATED ALWAYS AS (
                  setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
                  setweight(to_tsvector('english', coalesce(body,'')),  'B')) STORED,
  embedding     halfvec(384) NOT NULL,
  crawled_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX documents_tsv_idx ON documents USING gin (tsv);
CREATE INDEX documents_emb_idx ON documents USING hnsw (embedding halfvec_cosine_ops);

CREATE TABLE meta (key text PRIMARY KEY, value text);

CREATE ROLE crawler LOGIN PASSWORD :'crawler_pw';
CREATE ROLE api     LOGIN PASSWORD :'api_pw';

GRANT CONNECT ON DATABASE antiseo TO crawler, api;
GRANT USAGE ON SCHEMA public TO crawler, api;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crawler;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO crawler;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO api;
