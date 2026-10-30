-- One thumbnail per document. Run as the admin user against `antiseo`:
--   psql "host=$PGHOST dbname=antiseo user=pgadmin sslmode=require" -v ON_ERROR_STOP=1 -f db/002_images.sql
CREATE TABLE images (
  id           bigserial PRIMARY KEY,
  document_id  bigint NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  source_url   text NOT NULL,            -- where the original image lives
  blob_path    text NOT NULL,            -- e.g. ab/ab12...ef.webp in the thumbs container
  width        int NOT NULL,
  height       int NOT NULL,
  alt          text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX images_blob_path_idx ON images (blob_path);

GRANT SELECT, INSERT, UPDATE, DELETE ON images TO crawler;
GRANT USAGE, SELECT ON SEQUENCE images_id_seq TO crawler;
GRANT SELECT ON images TO api;
