"""
On-demand, single-restaurant dish lookup — the "lookup" product shape.

batch_extract.py and aggregate.py's --all runs assume you want dish
profiles for every restaurant in the dataset, computed upfront. That only
makes sense if the product needs to answer city-wide questions ("best pad
thai in Philly"). If the product is instead "look up this one restaurant's
dishes," extracting every restaurant nobody will ever search for is pure
waste — most of a 5,855-restaurant city's businesses may never get looked
up at all.

This script does the same extraction + aggregation work as the two
scripts above, but scoped to exactly one restaurant, run at the moment
someone actually asks for it. It reuses their extraction/aggregation
logic directly (run_extraction, aggregate_business) rather than
duplicating it — the difference is entirely in *when* and *for whom* the
work happens, not what the work is.

Every review is still only ever extracted once, ever: the extracted_at
tracking is the same column batch_extract.py uses, so looking up the same
restaurant twice does zero extra API work the second time — it's a plain
Postgres read.

Usage:
    python -m pipeline.lookup_restaurant --name "Zahav"
    python -m pipeline.lookup_restaurant --business-id 42
"""

import argparse
import os

import psycopg
import voyageai
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

from pipeline.aggregate import aggregate_business
from pipeline.batch_extract import DEFAULT_CONCURRENCY, fetch_pending_reviews, run_extraction

load_dotenv()

MAX_MATCHES_SHOWN = 20


def find_businesses(cur, name: str | None, business_id: int | None) -> list[tuple[int, str, str, str, str, str]]:
    columns = "id, name, address, city, state, postal_code"
    if business_id is not None:
        cur.execute(f"SELECT {columns} FROM businesses WHERE id = %s", (business_id,))
    else:
        cur.execute(f"SELECT {columns} FROM businesses WHERE name ILIKE %s ORDER BY name", (f"%{name}%",))
    return cur.fetchall()


def format_match(biz_id: int, name: str, address: str | None, city: str, state: str, postal_code: str | None) -> str:
    location = ", ".join(part for part in (address, city, state, postal_code) if part)
    return f"{name} — {location}" if location else name


def prompt_for_selection(matches: list[tuple]) -> tuple | None:
    """Shows a numbered list of candidate businesses and asks the user to
    pick one. Returns the chosen row, or None if the input wasn't usable
    (not interactive, invalid entry, or the user backed out) — callers
    should fall back to telling the user to re-run with --business-id
    rather than guessing."""
    shown, overflow = matches[:MAX_MATCHES_SHOWN], matches[MAX_MATCHES_SHOWN:]
    print(f"{len(matches)} restaurants match:")
    for i, row in enumerate(shown, start=1):
        print(f"  [{i}] {format_match(*row)}")
    if overflow:
        print(f"  ...and {len(overflow)} more — refine your search to see them")

    try:
        choice = input(f"Enter a number (1-{len(shown)}), or anything else to cancel: ").strip()
    except EOFError:
        print("\nNo input available — re-run with --business-id instead.")
        return None

    if not choice.isdigit() or not (1 <= int(choice) <= len(shown)):
        print("Cancelled.")
        return None
    return shown[int(choice) - 1]


def read_cached_dishes(cur, business_id: int) -> list[dict]:
    cur.execute(
        "SELECT name, mention_count, positive_count, negative_count, tags FROM dishes WHERE business_id = %s",
        (business_id,),
    )
    return [
        {"name": name, "mention_count": mc, "positive_count": pc, "negative_count": nc, "tags": tags}
        for name, mc, pc, nc, tags in cur.fetchall()
    ]


def get_dish_profiles(conn, business_id: int, concurrency: int) -> list[dict]:
    """Ensures a business's reviews are extracted and aggregated, and
    returns its current dish profiles — doing as little real work as
    possible on a repeat lookup. Shared by this script's raw listing and
    pipeline.recommend's ranked recommendations, so the on-demand
    extraction/aggregation behavior lives in exactly one place.

    Extraction only runs for reviews that are still pending — a cost paid
    once per review, ever. Aggregation is more subtle: it's cheap per call
    (Voyage, not Claude) but NOT free, and pipeline.aggregate re-embeds
    every distinct dish name from scratch on every call. Re-running it on
    a lookup that found zero new reviews would re-embed and re-cluster the
    exact same names for no new information — multiply that by every
    repeat lookup of a popular restaurant and it adds up to real,
    avoidable Voyage request volume (this is what was silently happening
    here before, and is the likely actual cause of hitting Voyage's free
    rate limits from what looks like light traffic). So: only aggregate
    when there's something new to aggregate; otherwise read the `dishes`
    rows a previous lookup already computed.
    """
    with conn.cursor() as cur:
        reviews = fetch_pending_reviews(cur, business_id, limit=None)

    if reviews:
        print(f"First lookup — extracting {len(reviews)} review(s) now...")
        processed, new_mentions, failed = run_extraction(conn, reviews, concurrency)
        print(f"Extracted {processed} review(s), {new_mentions} new dish mention(s), {failed} failure(s)")
        voyage_client = voyageai.Client()  # reads VOYAGE_API_KEY from the environment
        return aggregate_business(conn, business_id, voyage_client, verbose=False)

    with conn.cursor() as cur:
        cached = read_cached_dishes(cur, business_id)
    if cached:
        print("Already extracted and aggregated from a previous lookup — reading cached results, no API calls made.")
        return cached

    # Reviews were extracted (maybe by a previous run that was interrupted
    # before aggregating) but dishes were never computed — aggregate once now.
    print("Reviews already extracted but not yet aggregated — aggregating now...")
    voyage_client = voyageai.Client()
    return aggregate_business(conn, business_id, voyage_client, verbose=False)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="Restaurant name (partial match, case-insensitive)")
    group.add_argument("--business-id", type=int, help="Look up by internal id directly")
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"How many of this restaurant's reviews to send to Claude at once (default: {DEFAULT_CONCURRENCY})",
    )
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as conn:
        register_vector(conn)

        with conn.cursor() as cur:
            matches = find_businesses(cur, args.name, args.business_id)

        if not matches:
            raise SystemExit("No matching restaurant found.")
        if len(matches) == 1:
            chosen = matches[0]
        else:
            chosen = prompt_for_selection(matches)
            if chosen is None:
                raise SystemExit("Be more specific, or pass --business-id directly.")

        business_id, name, address, city, state, postal_code = chosen
        print(f"{format_match(*chosen)} — business_id={business_id}")

        profiles = get_dish_profiles(conn, business_id, args.concurrency)

    if not profiles:
        print("No dish mentions found for this restaurant.")
        return

    print(f"\n{name} — {len(profiles)} dish(es):")
    for p in sorted(profiles, key=lambda p: -p["mention_count"]):
        tags = ", ".join(p["tags"]) if p["tags"] else "—"
        print(
            f"  {p['name']}: {p['mention_count']} mention(s) "
            f"({p['positive_count']} positive, {p['negative_count']} negative) — tags: {tags}"
        )


if __name__ == "__main__":
    main()
