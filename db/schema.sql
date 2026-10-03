-- Phase 1 schema, extended in phase 2 for batch extraction and
-- aggregation: reviews.extracted_at tracks which reviews have already been
-- run through pipeline.batch_extract, and dishes.embedding now gets
-- populated (a mean-pooled Voyage embedding per aggregated dish profile)
-- by pipeline.aggregate. businesses.address/postal_code were added so
-- pipeline.lookup_restaurant can disambiguate same-named restaurants.
--
-- Phase 3 added dishes.blurb: pipeline.recommend's generated "why order
-- this" text, cached so a repeat recommendation request doesn't pay for
-- another Opus call. It rides along with the rest of a dishes row, so it
-- naturally comes back NULL (and gets regenerated) whenever
-- pipeline.aggregate recomputes that row — see get_or_generate_blurb.
--
-- If your database already exists from phase 1, add the new columns with:
--   docker compose exec -T db psql -U postgres -d dishfinder \
--       -c "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMPTZ;
--           ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address TEXT;
--           ALTER TABLE businesses ADD COLUMN IF NOT EXISTS postal_code TEXT;
--           ALTER TABLE dishes ADD COLUMN IF NOT EXISTS blurb TEXT;"
-- Then re-run pipeline.load_data on your city's filtered files to backfill
-- address/postal_code on already-loaded businesses.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS businesses (
    id                  SERIAL PRIMARY KEY,
    yelp_business_id    TEXT UNIQUE NOT NULL,
    name                TEXT NOT NULL,
    address             TEXT,
    city                TEXT,
    state               TEXT,
    postal_code         TEXT,
    categories          TEXT[]
);

CREATE TABLE IF NOT EXISTS reviews (
    id                  SERIAL PRIMARY KEY,
    business_id         INTEGER NOT NULL REFERENCES businesses(id),
    yelp_review_id      TEXT UNIQUE NOT NULL,
    stars               SMALLINT,
    review_text         TEXT NOT NULL,
    review_date         DATE,
    -- Set by pipeline.batch_extract once a review has been run through
    -- extraction, whether or not it mentioned any dishes. Lets re-runs
    -- pick up only the reviews they haven't processed yet.
    extracted_at        TIMESTAMPTZ
);

-- One row per (review, dish mention) — the raw output of the extraction
-- agent, before any aggregation across reviews.
CREATE TABLE IF NOT EXISTS dish_extractions (
    id                  SERIAL PRIMARY KEY,
    review_id           INTEGER NOT NULL REFERENCES reviews(id),
    business_id         INTEGER NOT NULL REFERENCES businesses(id),
    dish_name           TEXT NOT NULL,
    sentiment           TEXT NOT NULL CHECK (sentiment IN ('positive', 'negative', 'neutral')),
    tags                TEXT[] DEFAULT '{}'
);

-- Aggregated per-business dish profile. Populated by pipeline.aggregate,
-- which fully recomputes a business's rows from its dish_extractions each
-- run (see that module's docstring for why) — not written to directly by
-- extract.py or batch_extract.py.
CREATE TABLE IF NOT EXISTS dishes (
    id                  SERIAL PRIMARY KEY,
    business_id         INTEGER NOT NULL REFERENCES businesses(id),
    name                TEXT NOT NULL,
    mention_count        INTEGER NOT NULL DEFAULT 0,
    positive_count      INTEGER NOT NULL DEFAULT 0,
    negative_count       INTEGER NOT NULL DEFAULT 0,
    tags                TEXT[] DEFAULT '{}',
    embedding            vector(1024),
    -- Cached output of pipeline.recommend's Opus call for this dish.
    -- NULL until first requested; see get_or_generate_blurb.
    blurb                TEXT,
    UNIQUE (business_id, name)
);

CREATE INDEX IF NOT EXISTS reviews_business_id_idx ON reviews(business_id);
CREATE INDEX IF NOT EXISTS dish_extractions_business_id_idx ON dish_extractions(business_id);
