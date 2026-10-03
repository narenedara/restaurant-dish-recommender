"""
Rolls per-review dish_extractions into per-business dish profiles.

This is phase 2's aggregation step. dish_extractions has one row per
(review, dish mention) as written by pipeline.batch_extract — raw, one
row per mention, full of near-duplicate names ("birria taco" vs "birria
tacos" vs "the birria"). This script groups those into the dishes table:
one row per distinct dish per business, with mention/sentiment counts, a
merged tag list, and an embedding for later semantic search.

Matching near-duplicate names happens in two passes:
1. Exact match, case/whitespace-insensitive — cheap, and collapses the
   large majority of duplicates (the same dish typed the same way many
   times) before any embedding calls happen.
2. Embedding similarity (Voyage), over just the *distinct* names left
   after pass 1, clustered with a similarity-threshold union-find. This is
   what catches genuinely different strings for the same dish — "birria
   taco" vs "birria tacos" vs "birria".

Each run fully recomputes a business's dishes rows from its
dish_extractions (delete, then re-insert) rather than updating existing
rows in place. Embedding-based clustering can draw slightly different
group boundaries as new extractions come in, so trying to reconcile that
against previously-written rows would risk stale or duplicate dish
profiles; recomputing from the raw extractions every time is simpler and
always consistent with them.

Usage:
    python -m pipeline.aggregate --business-id 3
    python -m pipeline.aggregate --all
"""

import argparse
import os
from collections import Counter, defaultdict

import numpy as np
import psycopg
import voyageai
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

load_dotenv()

EMBED_MODEL = "voyage-4-lite"  # 1024-dim output by default (matches dishes.embedding); current-gen, cheaper than voyage-3, and has a free-tier token allowance voyage-3 doesn't
SIMILARITY_THRESHOLD = 0.87  # cosine similarity above which two distinct dish names are treated as the same dish
EMBED_BATCH_SIZE = 1000  # Voyage's hard cap on texts per embed() request — a busy restaurant (thousands of reviews) can easily have more distinct dish-name strings than this before fuzzy clustering ever runs
MAX_TAGS = 5  # how many of the most common tags to keep per dish profile


def fetch_business_ids(cur, business_id: int | None) -> list[int]:
    if business_id is not None:
        return [business_id]
    cur.execute("SELECT id FROM businesses ORDER BY id")
    return [row[0] for row in cur.fetchall()]


def fetch_extractions(cur, business_id: int) -> list[tuple[str, str, list[str]]]:
    cur.execute(
        "SELECT dish_name, sentiment, tags FROM dish_extractions WHERE business_id = %s",
        (business_id,),
    )
    return cur.fetchall()


def group_by_normalized_name(rows: list[tuple[str, str, list[str]]]) -> dict[str, dict]:
    """Pass 1: collapse exact-match duplicates (case/whitespace-insensitive)
    before any embedding calls. Returns normalized name -> aggregated
    stats, including a count of every raw surface form seen so a canonical
    display name can be picked later."""
    groups: dict[str, dict] = {}
    for dish_name, sentiment, tags in rows:
        key = " ".join(dish_name.split()).lower()
        group = groups.setdefault(
            key,
            {
                "mention_count": 0,
                "positive_count": 0,
                "negative_count": 0,
                "tag_counts": Counter(),
                "surface_forms": Counter(),
            },
        )
        group["mention_count"] += 1
        if sentiment == "positive":
            group["positive_count"] += 1
        elif sentiment == "negative":
            group["negative_count"] += 1
        group["tag_counts"].update(tags)
        group["surface_forms"][dish_name.strip()] += 1
    return groups


def cluster_by_embedding(embeddings: list[list[float]], threshold: float) -> list[list[int]]:
    """Pass 2: union-find over cosine similarity between distinct names'
    embeddings. Still O(n^2) *comparisons* — clustering fundamentally
    means checking every pair — but computed as one vectorized similarity
    matrix (numpy/BLAS) instead of a pure-Python double loop. That's the
    difference between seconds and tens of minutes once a business has
    thousands of distinct dish-name strings before clustering collapses
    them (a business with thousands of reviews easily gets there — e.g.
    ~3,500 for a 3,000-review restaurant). Would still need a proper
    vector index (not just a faster matrix) to scale another couple of
    orders of magnitude beyond that."""
    n = len(embeddings)
    matrix = np.asarray(embeddings, dtype=np.float32)
    unit_vectors = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    similarity = unit_vectors @ unit_vectors.T  # cosine similarity, since rows are unit-normalized

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[root_i] = root_j

    # Upper triangle only — the similarity matrix is symmetric, and the
    # diagonal (each name vs. itself) is meaningless for clustering.
    above_threshold = np.argwhere(np.triu(similarity, k=1) >= threshold)
    for i, j in above_threshold:
        union(int(i), int(j))

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)
    return list(clusters.values())


def embed_all(voyage_client: voyageai.Client, texts: list[str]) -> list[list[float]]:
    """Embeds an arbitrary number of texts, chunking into requests no
    larger than Voyage's per-request text-count cap. A restaurant with
    thousands of reviews can have thousands of distinct dish-name strings
    left after exact-match grouping (fuzzy clustering hasn't run yet at
    this point — that's what these embeddings are for), so a single
    embed() call isn't always enough."""
    embeddings: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        chunk = texts[i : i + EMBED_BATCH_SIZE]
        result = voyage_client.embed(chunk, model=EMBED_MODEL, input_type="document")
        embeddings.extend(result.embeddings)
    return embeddings


def build_dish_profiles(groups: dict[str, dict], voyage_client: voyageai.Client) -> list[dict]:
    keys = list(groups.keys())
    if not keys:
        return []

    embeddings = embed_all(voyage_client, keys)

    clusters = cluster_by_embedding(embeddings, SIMILARITY_THRESHOLD)

    profiles = []
    for member_indices in clusters:
        member_groups = [groups[keys[i]] for i in member_indices]
        member_embeddings = [embeddings[i] for i in member_indices]

        canonical_name_counts: Counter = Counter()
        tag_counts: Counter = Counter()
        mention_count = positive_count = negative_count = 0
        for g in member_groups:
            mention_count += g["mention_count"]
            positive_count += g["positive_count"]
            negative_count += g["negative_count"]
            tag_counts.update(g["tag_counts"])
            canonical_name_counts.update(g["surface_forms"])

        canonical_name = canonical_name_counts.most_common(1)[0][0]
        top_tags = [tag for tag, _ in tag_counts.most_common(MAX_TAGS)]
        # Mean-pool the distinct member names' embeddings into one
        # representative vector for this dish profile.
        dim = len(member_embeddings[0])
        avg_embedding = [sum(e[d] for e in member_embeddings) / len(member_embeddings) for d in range(dim)]

        profiles.append(
            {
                "name": canonical_name,
                "mention_count": mention_count,
                "positive_count": positive_count,
                "negative_count": negative_count,
                "tags": top_tags,
                "embedding": avg_embedding,
            }
        )
    return profiles


def write_dish_profiles(cur, business_id: int, profiles: list[dict]) -> None:
    # Recompute from scratch each run rather than upserting — see module
    # docstring for why.
    cur.execute("DELETE FROM dishes WHERE business_id = %s", (business_id,))
    for p in profiles:
        cur.execute(
            """
            INSERT INTO dishes (business_id, name, mention_count, positive_count, negative_count, tags, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                business_id,
                p["name"],
                p["mention_count"],
                p["positive_count"],
                p["negative_count"],
                p["tags"],
                p["embedding"],
            ),
        )


def aggregate_business(conn, business_id: int, voyage_client: voyageai.Client, verbose: bool = True) -> list[dict]:
    """Aggregates one business's dish_extractions into dishes rows and
    returns the resulting profiles. Shared by this script's --all/
    --business-id runs and pipeline.lookup_restaurant's on-demand lookups."""
    with conn.cursor() as cur:
        rows = fetch_extractions(cur, business_id)
    if not rows:
        return []
    groups = group_by_normalized_name(rows)
    profiles = build_dish_profiles(groups, voyage_client)
    with conn.cursor() as cur:
        write_dish_profiles(cur, business_id, profiles)
    conn.commit()
    if verbose:
        print(f"business {business_id}: {len(rows)} extractions -> {len(profiles)} dish profile(s)")
    return profiles


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--business-id", type=int, help="Aggregate a single business")
    group.add_argument("--all", action="store_true", help="Aggregate every business")
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]
    voyage_client = voyageai.Client()  # reads VOYAGE_API_KEY from the environment

    with psycopg.connect(database_url) as conn:
        register_vector(conn)  # lets python lists be inserted directly into the vector column
        with conn.cursor() as cur:
            business_ids = fetch_business_ids(cur, args.business_id)
        for business_id in business_ids:
            aggregate_business(conn, business_id, voyage_client)

    print("Done.")


if __name__ == "__main__":
    main()
