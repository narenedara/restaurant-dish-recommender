"""
Runs dish extraction over many reviews and persists the results.

pipeline/extract.py proves the extraction call works on one review at a
time and only prints its output — nothing gets written to the database.
This script is what actually populates dish_extractions at scale: it pulls
reviews that haven't been processed yet, runs pipeline.extract's
extract_dishes() on each one, and writes every dish mention as its own
row. pipeline.aggregate then rolls those rows into per-business dish
profiles.

A review is "pending" if reviews.extracted_at IS NULL. We stamp that
column after processing a review whether or not it mentioned any dishes,
so a review with zero dish mentions doesn't look pending forever and get
re-sent to Claude on every run. Each review commits independently, so a
run that dies partway through (rate limit, network blip, ctrl-C) leaves
already-processed reviews stamped and picks up cleanly where it left off
next time.

extract_dishes() is a network call to Claude — the slow part by far — so
--concurrency of them run in parallel via a thread pool. Only the
*results* touch Postgres, and always from the main thread, one review at
a time: psycopg cursors aren't safe to share across threads, and
serializing just the writes keeps the same one-commit-per-review semantics
as a fully sequential run, at a fraction of the wall-clock time. Sized to
a real dataset, this matters — at one review per API round-trip,
processing hundreds of thousands of reviews sequentially would take days;
running a few dozen in flight at once brings that down to a manageable
window. Tune --concurrency down if you hit rate limits, up if you have
plenty of headroom.

Usage:
    python -m pipeline.batch_extract --all
    python -m pipeline.batch_extract --business-id 3
    python -m pipeline.batch_extract --all --limit 200 --concurrency 20
"""

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg
from dotenv import load_dotenv

from pipeline.extract import extract_dishes

load_dotenv()

DEFAULT_CONCURRENCY = 10


def fetch_pending_reviews(
    cur, business_id: int | None, limit: int | None, max_per_business: int | None = None
) -> list[tuple[int, int, str]]:
    """Reviews still needing extraction. Two independent knobs:

    - limit: an overall cap across the whole result set (e.g. for a quick
      test run), applied last.
    - max_per_business: keep only the `max_per_business` *longest* pending
      reviews within each business, dropping the rest — not applied by
      default, so a single-restaurant lookup (pipeline.lookup_restaurant)
      still gets full coverage.

      Review length turns out to be a strong, free proxy for exactly what
      we care about, measured on this project's own already-extracted
      data: reviews under 200 characters mention zero dishes >35% of the
      time; reviews over 1,800 characters do less than 2% of the time,
      and average dish mentions per review rises from ~1.7 to ~9.5 across
      that range. There's no length short enough to *always* skip
      outright (even 25-50 character reviews still mention a dish about a
      third of the time) — so this ranks and caps per business rather
      than applying a hard universal floor. That also makes it low-risk:
      a business at or under the cap is entirely unaffected (true for
      most — the average Philadelphia restaurant here has ~117 reviews),
      and only businesses with far more reviews than that lose anything,
      specifically their least-informative ones first.
    """
    if max_per_business is not None:
        query = """
            SELECT id, business_id, review_text FROM (
                SELECT id, business_id, review_text,
                       row_number() OVER (
                           PARTITION BY business_id ORDER BY length(review_text) DESC, id
                       ) AS rn
                FROM reviews
                WHERE extracted_at IS NULL
        """
        params: list = []
        if business_id is not None:
            query += " AND business_id = %s"
            params.append(business_id)
        query += ") ranked WHERE rn <= %s ORDER BY business_id, id"
        params.append(max_per_business)
    else:
        query = "SELECT id, business_id, review_text FROM reviews WHERE extracted_at IS NULL"
        params = []
        if business_id is not None:
            query += " AND business_id = %s"
            params.append(business_id)
        query += " ORDER BY id"

    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)

    cur.execute(query, params)
    return cur.fetchall()


def write_extraction(cur, review_id: int, business_id: int, dishes: list) -> int:
    """Insert one review's dish mentions and stamp it as done. Returns the
    number of dish mentions written."""
    for mention in dishes:
        cur.execute(
            """
            INSERT INTO dish_extractions (review_id, business_id, dish_name, sentiment, tags)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (review_id, business_id, mention.dish, mention.sentiment, mention.tags),
        )
    cur.execute("UPDATE reviews SET extracted_at = now() WHERE id = %s", (review_id,))
    return len(dishes)


def run_extraction(conn, reviews: list[tuple[int, int, str]], concurrency: int) -> tuple[int, int, int]:
    """Extracts dishes for a list of (review_id, business_id, review_text)
    rows concurrently and writes results to the db as they come in. Shared
    by this script's --all/--business-id runs and pipeline.lookup_restaurant's
    single-business, on-demand runs. Returns (processed, dish_mentions, failed)."""
    processed = 0
    dish_mentions = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_review = {
            pool.submit(extract_dishes, review_text): (review_id, business_id)
            for review_id, business_id, review_text in reviews
        }
        for future in as_completed(future_to_review):
            review_id, business_id = future_to_review[future]
            try:
                result = future.result()
                with conn.cursor() as cur:
                    dish_mentions += write_extraction(cur, review_id, business_id, result.dishes)
                conn.commit()
                processed += 1
                if processed % 100 == 0:
                    print(f"  ...{processed}/{len(reviews)} processed")
            except Exception as exc:
                # Leave extracted_at NULL so this review is retried on
                # the next run instead of silently dropping its data.
                conn.rollback()
                failed += 1
                print(f"  review {review_id} failed, will retry next run: {exc}")
    return processed, dish_mentions, failed


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--business-id", type=int, help="Only process reviews for this business")
    group.add_argument("--all", action="store_true", help="Process pending reviews across all businesses")
    parser.add_argument("--limit", type=int, help="Process at most this many reviews (useful for a test run)")
    parser.add_argument(
        "--max-reviews-per-business", type=int, default=None,
        help="Cap reviews processed per business, keeping the longest/most-descriptive ones first. "
        "Recommended for --all discovery-mode runs at real-city scale (e.g. 150) — see this "
        "function's docstring and the README's cost notes for why. Not applied by default.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"How many reviews to send to Claude at once (default: {DEFAULT_CONCURRENCY})",
    )
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            reviews = fetch_pending_reviews(cur, args.business_id, args.limit, args.max_reviews_per_business)
        print(f"{len(reviews)} pending review(s) to process (concurrency={args.concurrency})")
        processed, dish_mentions, failed = run_extraction(conn, reviews, args.concurrency)

    print(f"Done. Processed {processed} review(s), found {dish_mentions} dish mention(s), {failed} failure(s).")


if __name__ == "__main__":
    main()
