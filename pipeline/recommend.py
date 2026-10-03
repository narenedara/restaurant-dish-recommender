"""
Ranks a restaurant's dish profiles and writes a short "why order this"
blurb for each of the top --top-n — the actual recommendation output, on
top of pipeline.lookup_restaurant's raw dish listing.

Reuses pipeline.lookup_restaurant's business lookup/disambiguation and
on-demand extraction (get_dish_profiles) rather than duplicating it — this
module adds exactly two things neither lookup_restaurant nor aggregate
provide: a ranking that decides which dishes are actually worth
recommending, and a written explanation for each one.

RANKING (v2 — still fully deterministic):
A dish needs at least --min-mentions total mentions, with at least one
non-neutral one, to be ranked at all — below that there's no sentiment
signal to work with at all.

Above that bar, dishes are ranked by a Bayesian-averaged score (the same
"IMDB weighted rating" approach used to keep a movie with 3 five-star
ratings from outranking one with 10,000 ratings averaging 4.8) rather
than raw positive ratio. A raw ratio treats a dish mentioned 3 times at
3/3 positive as *better* than one mentioned 14 times at 12/14 positive —
backwards, since the second has far more evidence behind a very similar
result. The fix: blend each dish's own ratio with the restaurant's
overall average ratio (across its own eligible dishes — no assumption
imported from anywhere else), weighted by how much evidence backs each
side. A dish with only a few mentions leans heavily on the restaurant's
average; one with dozens of mentions is judged almost entirely on its own
record. CONFIDENCE_WEIGHT controls how many "phantom votes" of the
restaurant's average get blended in — raise it for a stronger pull toward
abundance, lower it to trust small samples more.

This is explicitly a dial, not a cliff: a dish with few mentions still
gets ranked (once it clears --min-mentions) and can still place highly if
its own ratio is strong — it just isn't blindly trusted the way a raw
ratio would. That's a real improvement over v1's known gap (low-mention
dishes were previously excluded outright or trusted outright with nothing
in between), though it isn't a full solution to "a great dish that's
rarely ordered can't out-rank a merely-good, frequently-ordered one" —
CONFIDENCE_WEIGHT pulls in the opposite direction from that on purpose,
and would need a deliberately different mechanism (e.g. a separate
"hidden gem" pass) to fix, not attempted here.

BLURB GENERATION:
One Claude call per recommended dish (so at most --top-n per lookup —
cheap, and worth spending on a stronger model than the bulk-extraction
Haiku calls since this is the actual customer-facing output). The prompt
grounds the blurb in the real reviewer tags/sentiment for that dish at
this specific restaurant — the aggregated ground truth — and allows the
model's general knowledge of the dish in only to explain or support that
signal, never to override it.

Usage:
    python -m pipeline.recommend --name "Zahav"
    python -m pipeline.recommend --business-id 34 --top-n 5
    python -m pipeline.recommend --business-id 34 --min-mentions 5
"""

import argparse
import os

import psycopg
from anthropic import Anthropic
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from pydantic import BaseModel

from pipeline.batch_extract import DEFAULT_CONCURRENCY
from pipeline.categories import CATEGORY_KEYWORDS, filter_by_categories
from pipeline.lookup_restaurant import find_businesses, format_match, get_dish_profiles, prompt_for_selection

load_dotenv()

# Low-volume (at most --top-n calls per lookup) and this is the actual
# customer-facing output, unlike the high-volume bulk extraction — worth
# the better model.
MODEL = "claude-opus-5"

MIN_MENTIONS_FOR_RANKING = 3  # a starting point, not a derived number — tune with --min-mentions
CONFIDENCE_WEIGHT = 5  # "phantom votes" of the restaurant's average blended into each dish's score — tune with --confidence-weight

RECOMMENDATION_SYSTEM_PROMPT = """You are writing a short recommendation \
blurb for one dish at a specific restaurant, to help a customer decide \
what to order. You'll be given the dish name, how many reviewers \
mentioned it, how many were positive vs. negative, and the most common \
descriptive tags reviewers used.

Ground your blurb primarily in that reviewer data — the tags and \
sentiment are real signal from people who actually ate there, and take \
priority over everything else. You may also draw on your general \
knowledge of what this dish typically is (ingredients, preparation, what \
makes a good version of it) to add helpful context, but only to support \
or explain the reviewer feedback — never to override it. If your general \
knowledge of the dish conflicts with what reviewers reported, trust the \
reviewers and don't call out the discrepancy.

Write 1-3 sentences, appetizing but honest — if there's meaningful \
negative feedback mixed in, note it briefly rather than pretend the dish \
is flawless."""


class DishBlurb(BaseModel):
    blurb: str


def rank_dishes(profiles: list[dict], min_mentions: int, confidence_weight: float = CONFIDENCE_WEIGHT) -> list[dict]:
    eligible = [
        p for p in profiles
        if p["mention_count"] >= min_mentions and (p["positive_count"] + p["negative_count"]) > 0
    ]
    if not eligible:
        return []

    def positive_ratio(p: dict) -> float:
        return p["positive_count"] / (p["positive_count"] + p["negative_count"])

    # The prior every dish's score gets pulled toward: this restaurant's
    # own ratio across its eligible dishes, not a hardcoded assumption
    # about what a "typical" rating looks like. Pooled across total votes
    # (sum of positives / sum of votes) rather than averaged per-dish —
    # a restaurant with many single-mention dishes shouldn't have its
    # prior dominated by dish *count* when a handful of dishes carry most
    # of the actual evidence (votes).
    total_positive = sum(p["positive_count"] for p in eligible)
    total_votes = sum(p["positive_count"] + p["negative_count"] for p in eligible)
    restaurant_avg_ratio = total_positive / total_votes

    def confidence_score(p: dict) -> float:
        votes = p["positive_count"] + p["negative_count"]
        weight_on_own_ratio = votes / (votes + confidence_weight)
        return weight_on_own_ratio * positive_ratio(p) + (1 - weight_on_own_ratio) * restaurant_avg_ratio

    return sorted(eligible, key=lambda p: (confidence_score(p), p["mention_count"]), reverse=True)


def generate_blurb(business_name: str, dish: dict) -> str:
    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    tags = ", ".join(dish["tags"]) if dish["tags"] else "no specific tags recorded"
    user_content = (
        f"Restaurant: {business_name}\n"
        f"Dish: {dish['name']}\n"
        f"Mentions: {dish['mention_count']} ({dish['positive_count']} positive, "
        f"{dish['negative_count']} negative)\n"
        f"Common tags from reviewers: {tags}"
    )
    response = client.messages.parse(
        model=MODEL,
        max_tokens=300,
        system=RECOMMENDATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=DishBlurb,
        # Writing 1-3 sentences from facts we hand it directly doesn't need
        # Opus's default adaptive thinking — low effort measured ~25% fewer
        # output tokens with no drop in blurb quality (see recommend.py's
        # cost notes in the README).
        output_config={"effort": "low"},
    )
    return response.parsed_output.blurb


def get_or_generate_blurb(conn, business_id: int, business_name: str, dish: dict) -> str:
    """Reads a cached blurb for this dish if one's already been generated;
    calls Opus and persists the result otherwise. Without this, every
    recommendation request — even a repeat visit to a restaurant that's
    fully cached everywhere else — would call Opus again for no new
    information, the same mistake pipeline.lookup_restaurant's
    get_dish_profiles fixed for Voyage embedding calls.

    dishes rows are fully recomputed on re-aggregation (pipeline.aggregate
    deletes and re-inserts, not updates in place), so a blurb naturally
    comes back NULL — and gets regenerated — exactly when the underlying
    dish profile actually changed. That's the right behavior: a stale
    blurb written against last month's tags/counts shouldn't survive a
    re-aggregation unchanged.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT blurb FROM dishes WHERE business_id = %s AND name = %s", (business_id, dish["name"]))
        row = cur.fetchone()
    if row and row[0]:
        return row[0]

    blurb = generate_blurb(business_name, dish)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dishes SET blurb = %s WHERE business_id = %s AND name = %s",
            (blurb, business_id, dish["name"]),
        )
    conn.commit()

    return blurb


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="Restaurant name (partial match, case-insensitive)")
    group.add_argument("--business-id", type=int, help="Look up by internal id directly")
    parser.add_argument(
        "--top-n", type=int, default=10, choices=range(1, 11), metavar="[1-10]",
        help="How many dishes to recommend (default: 10)",
    )
    parser.add_argument(
        "--min-mentions", type=int, default=MIN_MENTIONS_FOR_RANKING,
        help=f"Minimum mentions for a dish to be eligible for ranking (default: {MIN_MENTIONS_FOR_RANKING})",
    )
    parser.add_argument(
        "--confidence-weight", type=float, default=CONFIDENCE_WEIGHT,
        help="How many 'phantom votes' of the restaurant's average ratio get blended into each dish's "
        f"score — higher rewards abundance more strongly, lower trusts small samples more (default: {CONFIDENCE_WEIGHT})",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"How many of this restaurant's reviews to send to Claude at once, if extraction is needed (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--categories", help=f"Comma-separated categories to filter to (any match), from: {', '.join(CATEGORY_KEYWORDS)}",
    )
    args = parser.parse_args()
    selected_categories = [c.strip() for c in args.categories.split(",")] if args.categories else []

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

        if selected_categories:
            profiles = filter_by_categories(profiles, selected_categories)
            if not profiles:
                print(f"No dishes match categories: {', '.join(selected_categories)}")
                return

        ranked = rank_dishes(profiles, args.min_mentions, args.confidence_weight)
        if not ranked:
            print(
                f"\nNo dish has {args.min_mentions}+ mentions with clear sentiment yet — "
                "not enough signal to recommend confidently. Try --min-mentions with a lower number."
            )
            return

        top = ranked[: args.top_n]
        if len(top) < args.top_n:
            print(f"\nOnly {len(top)} dish(es) meet the {args.min_mentions}+ mention bar (asked for {args.top_n}).")

        print(f"\nTop {len(top)} at {name}:\n")
        for i, dish in enumerate(top, start=1):
            total_sentiment = dish["positive_count"] + dish["negative_count"]
            blurb = get_or_generate_blurb(conn, business_id, name, dish)
            print(
                f"{i}. {dish['name']}  "
                f"({dish['positive_count']}/{total_sentiment} positive, {dish['mention_count']} mention(s))"
            )
            print(f"   {blurb}\n")


if __name__ == "__main__":
    main()
