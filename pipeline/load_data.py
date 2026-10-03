"""
Loads business and review data into Postgres.

Reads newline-delimited JSON files (one JSON object per line — this is the
format the real Yelp Open Dataset ships in, and the sample files here match
it). Reading line by line instead of json.load()-ing the whole file means
this script uses roughly the same amount of memory whether the file is
5KB (the sample data) or 5GB (the real dataset).

Usage:
    python -m pipeline.load_data \
        --businesses data/sample/business_sample.json \
        --reviews data/sample/review_sample.json
"""

import argparse
import json
import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def load_businesses(cur, path: str) -> dict[str, int]:
    """Insert businesses, return a map of yelp_business_id -> our internal id.

    We need that map because the review file references businesses by
    Yelp's id, but our reviews table's foreign key points at our own
    internal integer id.
    """
    yelp_id_to_internal_id = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            cur.execute(
                """
                INSERT INTO businesses (yelp_business_id, name, address, city, state, postal_code, categories, yelp_stars, yelp_review_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (yelp_business_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    address = EXCLUDED.address,
                    postal_code = EXCLUDED.postal_code,
                    yelp_stars = EXCLUDED.yelp_stars,
                    yelp_review_count = EXCLUDED.yelp_review_count
                RETURNING id
                """,
                (
                    row["business_id"],
                    row["name"],
                    row.get("address"),
                    row.get("city"),
                    row.get("state"),
                    row.get("postal_code"),
                    [c.strip() for c in row.get("categories", "").split(",") if c.strip()],
                    row.get("stars"),
                    row.get("review_count"),
                ),
            )
            internal_id = cur.fetchone()[0]
            yelp_id_to_internal_id[row["business_id"]] = internal_id
    return yelp_id_to_internal_id


def load_reviews(cur, path: str, yelp_id_to_internal_id: dict[str, int]) -> int:
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            business_id = yelp_id_to_internal_id.get(row["business_id"])
            if business_id is None:
                # Review references a business we didn't load — skip it
                # rather than failing the whole run. On the real dataset
                # this happens constantly if you filter businesses to one
                # city but load all reviews.
                continue
            cur.execute(
                """
                INSERT INTO reviews (business_id, yelp_review_id, stars, review_text, review_date)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (yelp_review_id) DO NOTHING
                """,
                (business_id, row["review_id"], row.get("stars"), row["text"], row.get("date")),
            )
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--businesses", required=True, help="Path to business JSON lines file")
    parser.add_argument("--reviews", required=True, help="Path to review JSON lines file")
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            id_map = load_businesses(cur, args.businesses)
            print(f"Loaded {len(id_map)} businesses")

            review_count = load_reviews(cur, args.reviews, id_map)
            print(f"Loaded {review_count} reviews")

        conn.commit()

    print("Done.")


if __name__ == "__main__":
    main()
