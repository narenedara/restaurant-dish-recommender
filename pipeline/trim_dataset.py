"""
Shrinks what's already loaded in Postgres, for fitting a hosted database
into a storage budget (e.g. Supabase's free tier) — a different problem
from pipeline.filter_dataset's, which shrinks the *source* JSON files
before loading. This operates on rows already in the database.

Every operation here rebuilds the affected table with CREATE TABLE AS
SELECT + swap, rather than DELETE. This matters a lot in practice: DELETE
pays a per-row cost (WAL logging, index maintenance) proportional to rows
*removed*, while CTAS pays a cost proportional to rows *kept*. When
you're dropping the majority of a multi-million-row table — pruning
nationwide data down to a handful of cities, say — that difference is the
gap between a swap finishing in seconds and a DELETE running for the
better part of an hour (measured directly building this: the DELETE
version of this exact operation was killed after 30+ minutes of genuine,
non-stuck progress; the CTAS rewrite finished the same work in under 10
seconds).

Three operations, composable:

1. prune_to_cities: keep only businesses (and their reviews) in a given
   set of (city, state) pairs.

2. prune_low_rated: keep only businesses (and their reviews) at or above
   a Yelp star-rating threshold. Not just a storage lever — there's no
   reason to extract or recommend dishes from a restaurant reviewers
   broadly disliked, so this is a real quality filter, using Yelp's own
   crowd-sourced rating rather than anything this project computes.

3. cap_reviews_per_business: keep only the --max-reviews longest
   (most-descriptive — see pipeline.batch_extract's fetch_pending_reviews
   for the same reasoning applied at extraction time) reviews per
   business.

All three protect cached work the same way, for the same reason — this
project has already spent real money extracting and generating blurbs for
specific businesses/reviews, and no amount of dataset tidying should
silently throw that away:
  - prune_to_cities / prune_low_rated never drop a business that already
    has dish_extractions rows, regardless of city or rating (caught
    during manual testing: several real test restaurants — including ones
    demoed earlier in this project's own development — have Yelp ratings
    the default --min-stars would have dropped).
  - cap_reviews_per_business never drops an already-extracted review
    (dish_extractions may reference it), regardless of length rank.

Usage:
    python -m pipeline.trim_dataset \
        --keep-cities "Philadelphia:PA,New Orleans:LA" \
        --min-stars 3.0 \
        --max-reviews 40
"""

import argparse
import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def _swap_in_businesses(cur, keep_condition: str, params: tuple) -> tuple[int, int, int, int]:
    """Rebuilds businesses (and, by extension, reviews — anything not
    belonging to a kept business goes with it) to keep only rows matching
    `keep_condition`, a SQL boolean expression over the `businesses`
    table — plus any business with existing dish_extractions, always.
    Returns (businesses_kept, reviews_kept, dish_extractions_removed, dishes_removed).
    """
    cur.execute(
        f"""
        CREATE TABLE businesses_new AS
        SELECT * FROM businesses
        WHERE ({keep_condition}) OR id IN (SELECT DISTINCT business_id FROM dish_extractions)
        """,
        params,
    )
    cur.execute("CREATE TABLE reviews_new AS SELECT * FROM reviews WHERE business_id IN (SELECT id FROM businesses_new)")

    cur.execute("ALTER TABLE dish_extractions DROP CONSTRAINT dish_extractions_review_id_fkey")
    cur.execute("ALTER TABLE dish_extractions DROP CONSTRAINT dish_extractions_business_id_fkey")
    cur.execute("ALTER TABLE dishes DROP CONSTRAINT dishes_business_id_fkey")
    cur.execute("DROP TABLE reviews")
    cur.execute("DROP TABLE businesses")
    cur.execute("ALTER TABLE businesses_new RENAME TO businesses")
    cur.execute("ALTER TABLE reviews_new RENAME TO reviews")

    _restore_businesses_schema(cur)
    _restore_reviews_schema(cur)

    # Orphans: rows that referenced a business/review this filter dropped
    # and wasn't protected by the dish_extractions carve-out above (e.g.
    # leftover dishes rows with no matching dish_extractions, which
    # shouldn't normally happen but costs nothing to guard against).
    cur.execute("DELETE FROM dishes WHERE business_id NOT IN (SELECT id FROM businesses)")
    dishes_removed = cur.rowcount
    cur.execute(
        "DELETE FROM dish_extractions WHERE business_id NOT IN (SELECT id FROM businesses) OR review_id NOT IN (SELECT id FROM reviews)"
    )
    dish_extractions_removed = cur.rowcount

    cur.execute("ALTER TABLE reviews ADD CONSTRAINT reviews_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id)")
    cur.execute("ALTER TABLE dish_extractions ADD CONSTRAINT dish_extractions_review_id_fkey FOREIGN KEY (review_id) REFERENCES reviews(id)")
    cur.execute("ALTER TABLE dish_extractions ADD CONSTRAINT dish_extractions_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id)")
    cur.execute("ALTER TABLE dishes ADD CONSTRAINT dishes_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id)")

    cur.execute("SELECT count(*) FROM businesses")
    businesses_kept = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM reviews")
    reviews_kept = cur.fetchone()[0]
    return businesses_kept, reviews_kept, dish_extractions_removed, dishes_removed


def _restore_businesses_schema(cur) -> None:
    """CTAS copies column data only — PK, unique constraint, and the id
    sequence/default all need to be rebuilt by hand. DROP TABLE on the old
    businesses also drops businesses_id_seq (it's OWNED BY businesses.id),
    so the sequence has to be recreated, not just reused."""
    cur.execute("ALTER TABLE businesses ADD PRIMARY KEY (id)")
    cur.execute("ALTER TABLE businesses ADD CONSTRAINT businesses_yelp_business_id_key UNIQUE (yelp_business_id)")
    cur.execute("CREATE SEQUENCE businesses_id_seq OWNED BY businesses.id")
    cur.execute("ALTER TABLE businesses ALTER COLUMN id SET DEFAULT nextval('businesses_id_seq')")
    cur.execute("SELECT setval('businesses_id_seq', (SELECT COALESCE(MAX(id), 1) FROM businesses))")


def _restore_reviews_schema(cur) -> None:
    cur.execute("ALTER TABLE reviews ADD PRIMARY KEY (id)")
    cur.execute("ALTER TABLE reviews ADD CONSTRAINT reviews_yelp_review_id_key UNIQUE (yelp_review_id)")
    cur.execute("CREATE SEQUENCE reviews_id_seq OWNED BY reviews.id")
    cur.execute("ALTER TABLE reviews ALTER COLUMN id SET DEFAULT nextval('reviews_id_seq')")
    cur.execute("SELECT setval('reviews_id_seq', (SELECT COALESCE(MAX(id), 1) FROM reviews))")
    cur.execute("CREATE INDEX reviews_business_id_idx ON reviews(business_id)")


def prune_to_cities(cur, cities: list[tuple[str, str]]) -> tuple[int, int, int, int]:
    """Keeps only businesses (and their reviews) in `cities`, plus any
    business with existing dish_extractions regardless of city. Returns
    (businesses_kept, reviews_kept, dish_extractions_removed, dishes_removed)."""
    conditions = " OR ".join("(lower(city) = lower(%s) AND lower(state) = lower(%s))" for _ in cities)
    params = tuple(part for city, state in cities for part in (city, state))
    return _swap_in_businesses(cur, conditions, params)


def prune_low_rated(cur, min_stars: float) -> tuple[int, int, int, int]:
    """Keeps only businesses (and their reviews) at/above min_stars (or
    with no rating at all — a missing rating isn't evidence of a bad
    restaurant), plus any business with existing dish_extractions
    regardless of rating. Returns
    (businesses_kept, reviews_kept, dish_extractions_removed, dishes_removed)."""
    return _swap_in_businesses(cur, "yelp_stars IS NULL OR yelp_stars >= %s", (min_stars,))


def cap_reviews_per_business(cur, max_reviews: int) -> int:
    """Keeps only the `max_reviews` longest reviews per business, always
    keeping already-extracted ones regardless of rank or count. Returns
    the number of reviews removed."""
    cur.execute("SELECT count(*) FROM reviews")
    before = cur.fetchone()[0]

    cur.execute(
        """
        CREATE TABLE reviews_new AS
        SELECT id, business_id, yelp_review_id, stars, review_text, review_date, extracted_at
        FROM (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY business_id
                       ORDER BY (extracted_at IS NOT NULL) DESC, length(review_text) DESC
                   ) AS rn
            FROM reviews
        ) ranked
        WHERE rn <= %s OR extracted_at IS NOT NULL
        """,
        (max_reviews,),
    )
    cur.execute("ALTER TABLE dish_extractions DROP CONSTRAINT dish_extractions_review_id_fkey")
    cur.execute("DROP TABLE reviews")
    cur.execute("ALTER TABLE reviews_new RENAME TO reviews")
    _restore_reviews_schema(cur)
    cur.execute("ALTER TABLE reviews ADD CONSTRAINT reviews_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id)")
    cur.execute("ALTER TABLE dish_extractions ADD CONSTRAINT dish_extractions_review_id_fkey FOREIGN KEY (review_id) REFERENCES reviews(id)")

    cur.execute("SELECT count(*) FROM reviews")
    after = cur.fetchone()[0]
    return before - after


def parse_cities(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for entry in raw.split(","):
        city, _, state = entry.strip().partition(":")
        if not city or not state:
            raise SystemExit(f"Bad --keep-cities entry: {entry!r} — expected 'City:ST'")
        pairs.append((city.strip(), state.strip()))
    return pairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-cities", help="Comma-separated 'City:ST' pairs to keep — everything else is dropped")
    parser.add_argument("--min-stars", type=float, help="Keep only businesses at/above this Yelp rating")
    parser.add_argument("--max-reviews", type=int, help="Cap reviews per business at this many")
    args = parser.parse_args()

    if not args.keep_cities and args.min_stars is None and args.max_reviews is None:
        raise SystemExit("Pass --keep-cities, --min-stars, and/or --max-reviews — nothing to do otherwise.")

    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            if args.keep_cities:
                cities = parse_cities(args.keep_cities)
                businesses, reviews, de_removed, dishes_removed = prune_to_cities(cur, cities)
                print(
                    f"Pruned to {len(cities)} cities: {businesses} businesses, {reviews} reviews kept "
                    f"({de_removed} orphaned dish_extractions, {dishes_removed} orphaned dishes removed)"
                )

            if args.min_stars is not None:
                businesses, reviews, de_removed, dishes_removed = prune_low_rated(cur, args.min_stars)
                print(
                    f"Pruned below {args.min_stars} stars: {businesses} businesses, {reviews} reviews kept "
                    f"({de_removed} orphaned dish_extractions, {dishes_removed} orphaned dishes removed)"
                )

            if args.max_reviews is not None:
                removed = cap_reviews_per_business(cur, args.max_reviews)
                print(f"Capped at {args.max_reviews} reviews/business: {removed} reviews removed")

        conn.commit()

    print("Done.")


if __name__ == "__main__":
    main()
