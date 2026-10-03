"""
Phase 3's FastAPI layer — the "simple search" UX: type a restaurant name,
see its top-N ranked dishes with blurbs, in a browser instead of a
terminal.

This file adds no new business logic. Every endpoint is a thin wrapper
around functions that already exist and are already tested from the CLI:
pipeline.lookup_restaurant.find_businesses / get_dish_profiles, and
pipeline.recommend.rank_dishes / generate_blurb. If you want to see how
the actual recommendation works, read those modules — this one is just
the HTTP plumbing on top.

Serves the static frontend (frontend/index.html) from the same process as
the API, so the whole app is one process on one port with no CORS setup
needed.

Run:
    uvicorn api.main:app --reload
Then open http://localhost:8000 in a browser.
"""

import os

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pgvector.psycopg import register_vector
from pydantic import BaseModel

from pipeline.batch_extract import DEFAULT_CONCURRENCY
from pipeline.categories import available_categories, filter_by_categories
from pipeline.lookup_restaurant import find_businesses, get_dish_profiles
from pipeline.recommend import CONFIDENCE_WEIGHT, MIN_MENTIONS_FOR_RANKING, get_or_generate_blurb, rank_dishes

load_dotenv()

app = FastAPI(title="Restaurant Dish Recommender")


def get_connection() -> psycopg.Connection:
    """One connection per request — matches the CLI scripts' pattern
    (they each open one connection per invocation too). Fine for a local
    dev server; a connection pool would be the next step if this ever
    needs to handle real concurrent traffic."""
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    register_vector(conn)
    return conn


class BusinessMatch(BaseModel):
    business_id: int
    name: str
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None


class DishRecommendation(BaseModel):
    rank: int
    name: str
    mention_count: int
    positive_count: int
    negative_count: int
    tags: list[str]
    blurb: str


@app.get("/api/restaurants/search", response_model=list[BusinessMatch])
def search_restaurants(name: str = Query(..., min_length=1, description="Partial, case-insensitive restaurant name")):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            matches = find_businesses(cur, name, None)
    finally:
        conn.close()

    return [
        BusinessMatch(business_id=biz_id, name=biz_name, address=address, city=city, state=state, postal_code=postal_code)
        for biz_id, biz_name, address, city, state, postal_code in matches
    ]


@app.get("/api/restaurants/{business_id}/categories")
def get_categories(
    business_id: int,
    concurrency: int = Query(DEFAULT_CONCURRENCY, ge=1, le=50, description="Extraction concurrency, if this restaurant hasn't been looked up before"),
):
    """Category -> count of matching dishes, for populating filter chips.
    Only categories with at least one match are included, so the frontend
    never offers a chip that would return zero results for this
    restaurant. Shares get_dish_profiles's cache with /recommendations —
    calling both for the same restaurant costs nothing extra on repeat
    visits."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM businesses WHERE id = %s", (business_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="No restaurant with that id")

        profiles = get_dish_profiles(conn, business_id, concurrency)
    finally:
        conn.close()

    return available_categories(profiles)


@app.get("/api/restaurants/{business_id}/recommendations", response_model=list[DishRecommendation])
def get_recommendations(
    business_id: int,
    top_n: int = Query(10, ge=1, le=10, description="How many dishes to recommend"),
    min_mentions: int = Query(MIN_MENTIONS_FOR_RANKING, ge=1, description="Minimum mentions before a dish is eligible for ranking"),
    confidence_weight: float = Query(
        CONFIDENCE_WEIGHT, ge=0,
        description="'Phantom votes' of the restaurant's average blended into each dish's score — higher rewards abundance more",
    ),
    categories: str | None = Query(None, description="Comma-separated categories to filter to (any match) — see /categories for what's available"),
    concurrency: int = Query(DEFAULT_CONCURRENCY, ge=1, le=50, description="Extraction concurrency, if this restaurant hasn't been looked up before"),
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM businesses WHERE id = %s", (business_id,))
            row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No restaurant with that id")
        business_name = row[0]

        # The slow part on a first-ever lookup: extracts any pending
        # reviews with Claude, then aggregates into dish profiles. Cached
        # (near-instant) on every lookup after the first — see
        # get_dish_profiles's docstring.
        profiles = get_dish_profiles(conn, business_id, concurrency)
        if not profiles:
            return []

        # Filter before ranking, not after: a filtered-to-"spicy" request
        # wants the best spicy dish on the whole menu, which might not be
        # in the unfiltered top-N at all.
        if categories:
            profiles = filter_by_categories(profiles, [c.strip() for c in categories.split(",")])
            if not profiles:
                return []

        top = rank_dishes(profiles, min_mentions, confidence_weight)[:top_n]

        # get_or_generate_blurb needs conn (to read/write the cache), so
        # this has to happen before conn.close() below — same reason
        # get_dish_profiles does.
        return [
            DishRecommendation(
                rank=i,
                name=dish["name"],
                mention_count=dish["mention_count"],
                positive_count=dish["positive_count"],
                negative_count=dish["negative_count"],
                tags=dish["tags"],
                blurb=get_or_generate_blurb(conn, business_id, business_name, dish),
            )
            for i, dish in enumerate(top, start=1)
        ]
    finally:
        conn.close()


# Mounted last and at "/" so the specific /api/* routes above always match
# first — Starlette checks routes in registration order.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
