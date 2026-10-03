"""
Filters the full Yelp Open Dataset down to one city, so you aren't loading
millions of businesses/reviews on your first real run.

Same streaming approach as load_data.py, for the same reason: the full
dataset's review file is several GB, so this reads it one line at a time
rather than json.load()-ing the whole thing.

Two passes over the data:
1. Scan the business file for rows matching --city (and --state, if given,
   and --category, to skip non-restaurants), collecting their business_ids
   and writing the matching rows to <out-dir>/business.json.
2. Scan the review file, keeping only reviews whose business_id was
   collected in pass 1, writing them to <out-dir>/review.json.

The output files are the same newline-delimited JSON shape as the input
(and as data/sample/), so pipeline.load_data works on them unchanged.

Usage:
    python -m pipeline.filter_dataset \
        --businesses ~/Downloads/yelp_dataset/yelp_academic_dataset_business.json \
        --reviews ~/Downloads/yelp_dataset/yelp_academic_dataset_review.json \
        --city Philadelphia --state PA \
        --out-dir data/philadelphia
"""

import argparse
import json
import os


def filter_businesses(
    in_path: str, out_path: str, city: str | None, state: str | None, category: str | None
) -> set[str]:
    matched_ids: set[str] = set()
    city_lower = city.lower() if city else None
    with open(in_path, "r", encoding="utf-8") as in_f, open(out_path, "w", encoding="utf-8") as out_f:
        for line in in_f:
            row = json.loads(line)
            if city_lower and (row.get("city") or "").strip().lower() != city_lower:
                continue
            if state and (row.get("state") or "").strip().lower() != state.lower():
                continue
            if category and category.lower() not in (row.get("categories") or "").lower():
                continue
            matched_ids.add(row["business_id"])
            out_f.write(line if line.endswith("\n") else line + "\n")
    return matched_ids


def filter_reviews(in_path: str, out_path: str, business_ids: set[str]) -> int:
    count = 0
    with open(in_path, "r", encoding="utf-8") as in_f, open(out_path, "w", encoding="utf-8") as out_f:
        for line in in_f:
            row = json.loads(line)
            if row.get("business_id") not in business_ids:
                continue
            out_f.write(line if line.endswith("\n") else line + "\n")
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--businesses", required=True, help="Path to the full Yelp business JSON-lines file")
    parser.add_argument("--reviews", required=True, help="Path to the full Yelp review JSON-lines file")
    parser.add_argument(
        "--city", help="City name to filter to, e.g. Philadelphia. Omit (with --category) to keep every city"
    )
    parser.add_argument("--state", help="Two-letter state code to disambiguate same-named cities, e.g. PA")
    parser.add_argument(
        "--category",
        default="Restaurants",
        help="Only keep businesses whose categories contain this substring (default: Restaurants). "
        "Pass an empty string to disable category filtering.",
    )
    parser.add_argument("--out-dir", required=True, help="Directory to write business.json / review.json into")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    business_out = os.path.join(args.out_dir, "business.json")
    review_out = os.path.join(args.out_dir, "review.json")

    business_ids = filter_businesses(args.businesses, business_out, args.city, args.state, args.category)
    location = args.city or "every city"
    print(f"Matched {len(business_ids)} businesses in {location}{f', {args.state}' if args.state else ''}")
    if not business_ids:
        raise SystemExit("No businesses matched — check the city/state spelling against the raw data before continuing.")

    review_count = filter_reviews(args.reviews, review_out, business_ids)
    print(f"Matched {review_count} reviews for those businesses")
    print(f"Wrote {business_out} and {review_out}")


if __name__ == "__main__":
    main()
