"""
Pulls structured dish mentions out of one review's text using Claude.

This is the core "AI" step of the whole project — everything else (the
database, the pipeline, later LangGraph orchestration) exists to run this
extraction at scale and serve the results. Get this one call right first.

We use Claude's structured outputs feature (client.messages.parse with a
Pydantic model) rather than just asking for JSON in the prompt. The
response is *guaranteed* to match the schema below — no markdown fences to
strip, no malformed JSON to catch, no retry-on-bad-parse logic needed.

Usage:
    python -m pipeline.extract --text "The tacos were amazing but the salsa was bland."
    python -m pipeline.extract --review-id 1
"""

import argparse
import os
from typing import Literal

import psycopg
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"  # cheap + fast, right fit for high-volume extraction

SYSTEM_PROMPT = """You are extracting mentions of specific dishes from a \
restaurant review. For every distinct dish named in the review, record its \
name as written, whether the reviewer felt positive, negative, or neutral \
about it, and up to three short descriptive tags (e.g. "spicy", \
"overpriced", "generous portion"). Ignore vague mentions like "the food" \
or "the service" that don't name a specific dish. If no specific dish is \
named, return an empty list."""


class DishMention(BaseModel):
    dish: str
    sentiment: Literal["positive", "negative", "neutral"]
    tags: list[str]


class ExtractionResult(BaseModel):
    dishes: list[DishMention]


# Retries with exponential backoff. Worth having from day one: API calls
# fail transiently (rate limits, brief network blips) often enough that a
# pipeline without retries will fail partway through a real run.
@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
def extract_dishes(review_text: str) -> ExtractionResult:
    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    response = client.messages.parse(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": review_text}],
        output_format=ExtractionResult,
    )
    return response.parsed_output


def fetch_review_text(review_id: int) -> str:
    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT review_text FROM reviews WHERE id = %s", (review_id,))
            row = cur.fetchone()
            if row is None:
                raise SystemExit(f"No review with id {review_id}")
            return row[0]


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="Raw review text to test against, no database needed")
    group.add_argument("--review-id", type=int, help="Pull review text from the database by id")
    args = parser.parse_args()

    review_text = args.text if args.text else fetch_review_text(args.review_id)

    result = extract_dishes(review_text)
    for mention in result.dishes:
        print(f"- {mention.dish} ({mention.sentiment}): {', '.join(mention.tags)}")

    if not result.dishes:
        print("No specific dishes mentioned in this review.")


if __name__ == "__main__":
    main()
