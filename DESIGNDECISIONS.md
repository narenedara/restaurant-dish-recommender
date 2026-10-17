# Design decisions

This is a log of the non-obvious engineering decisions made building this
project, and the reasoning behind each one — written as they came up, not
reconstructed after the fact. Most of these were found by testing against
real data and real failure modes, not designed in the abstract up front.

## Cost control was the central constraint, and it shaped the architecture

The single biggest decision in the project: extract a restaurant's reviews
with Claude only the first time someone actually looks it up, cached in
Postgres forever after, instead of precomputing the whole dataset upfront.
Precomputing Philadelphia alone — 5,855 restaurants, 687K reviews — would
have cost an estimated **$480–690** in Claude API spend before a single user
ever saw a result, regardless of whether anyone actually wanted most of
those restaurants. The on-demand design means the real cost is **$0 until
someone asks**, and every restaurant is only ever extracted once.

Three further measures built on top of that foundation:

- **Caching generated content, not just raw extractions.** Found (via
  testing, not by inspection) that recommendation requests were silently
  re-paying for work on every call: `aggregate_business()` re-embedded every
  distinct dish name with Voyage on *every* lookup, even for a restaurant
  that hadn't changed, and `generate_blurb()` re-ran the Opus call for every
  recommended dish on every request. Both now check a cache column first
  (`dishes.blurb`, and `dishes` itself before re-aggregating) and only pay
  for new work when the underlying data actually changed.
- **Capping reviews per restaurant, by evidence, not a blind truncation.**
  A handful of restaurants (Zahav: 3,173 reviews) account for a
  disproportionate share of total volume. Before capping anything, measured
  the actual relationship between review length and how many dishes a
  review mentions, directly on this project's own data: reviews under 200
  characters mention zero dishes 55.5% of the time; reviews over 1,800
  characters do so only 1.4% of the time, and average dish mentions per
  review rises from 1.69 to 9.55 across that range. That justified ranking
  each business's reviews by length and keeping only the longest N — but
  also ruled out a flat "skip short reviews" filter, since even 25–50
  character reviews still mention a dish about a third of the time. The cap
  only engages for restaurants *above* the threshold, so a typical
  restaurant (~117 reviews, well under any reasonable cap) is never
  touched.
- **Deterministic computation over an LLM call, wherever the task doesn't
  need language understanding.** Both the ranking score and the dish
  category filter (spicy/hearty/light/sweet/comfort) are plain arithmetic
  and keyword matching, not model calls — free, instant, and fully
  explainable, at the cost of some precision an LLM classification pass
  would have. Both are documented as deliberate v1 choices with a clear
  upgrade path, not oversights.

## Restaurant disambiguation: surfacing address, not just picking one

Name-based restaurant search returns real collisions — "Royal Indian
Cuisine" matched both "Bombay Royal Indian Cuisine" and "The Royal Indian
Cuisine," two different restaurants a few blocks apart. Guessing which one
the user meant would silently serve the wrong restaurant's reviews.
Resolved by surfacing every match with its street address and letting the
user pick — a numbered picker in the CLI, a live dropdown with inline
address text in the browser. This exposed a real schema gap along the way:
`businesses` never stored address at all, so disambiguation needed a
backfill from the raw dataset before it could work.

## Dish ranking: from raw sentiment ratio to evidence-weighted scoring

The first version ranked dishes by raw positive ratio. That has an obvious
failure mode once there's real data to test it against: a dish mentioned 3
times at 3/3 positive outranks one mentioned 14 times at 12/14 positive,
even though the second has far more evidence behind a similar result.
Confirmed this with a real example — a restaurant where "chicken" (12/14
positive, 14 mentions) was ranked below 27 different dishes that happened
to have exactly one 5-star mention each.

Fixed with a Bayesian-averaged score (the same approach behind IMDB's "Top
250" weighted rating): each dish's own ratio is blended with the
restaurant's overall ratio, weighted by how much evidence backs it — a
dish with few mentions leans on the restaurant's baseline, one with dozens
is judged almost entirely on its own record. The blend weight
(`CONFIDENCE_WEIGHT = 5`, tunable) controls how strongly abundance is
rewarded, by design not "as drastic as possible" — a dish with few
mentions can still rank well if its own ratio is strong.

One subtlety caught during verification, not designed in up front: the
restaurant-wide baseline was first computed as a simple average of each
dish's ratio, which let a restaurant with many single-mention "lucky"
dishes inflate its own baseline and undermine the fix. Switched to a
vote-weighted pooled average (total positive mentions ÷ total mentions,
not averaged per dish) so the baseline reflects actual evidence volume,
not dish count.

**Open problem, not solved:** a dish that's genuinely excellent but rarely
ordered can't surface under this scheme — `CONFIDENCE_WEIGHT` explicitly
pulls in the *opposite* direction, toward trusting volume. Mention count
mostly reflects what's popular to order, not what's good, and fixing that
fairly needs a different mechanism (a confidence-adjusted score that
rewards rather than penalizes thin evidence, or a separate "hidden gem"
pass) — left as a known gap rather than attempting a half-measure.

## Guardrails against hallucinated dish descriptions

The recommendation blurb for each dish is LLM-generated, which creates an
obvious risk: the model inventing flavor or preparation details no
reviewer actually said. The system prompt explicitly ranks real reviewer
tags and sentiment as the primary source of truth, and permits the model's
general knowledge of a dish only to *explain or support* that signal, with
an explicit instruction not to surface a conflict if its own knowledge
disagrees with what reviewers reported. This was verified empirically, not
just assumed: when a generated blurb mentioned "za'atar" and "schug" for a
hummus dish — details that look like plausible hallucination for a Middle
Eastern dish — the exact prompt sent to the model was reconstructed and
confirmed both words were literally present in that dish's stored tags.

## Scaling the embedding pipeline: two independent limits found by testing, not reading docs

Dish-name deduplication clusters near-duplicate names ("birria taco" vs.
"birria tacos") using Voyage embeddings and cosine similarity
(`SIMILARITY_THRESHOLD = 0.87`). Tested against the project's smallest
realistic case (a handful of businesses) and it worked fine — then broke
on the first real high-volume restaurant (Zahav, 3,173 reviews):

- **Voyage's embed() call has a hard cap of 1,000 texts per request.**
  Zahav alone had 3,543 distinct dish-name strings before clustering ever
  ran (the exact-match pass only collapses byte-identical duplicates).
  Fixed by chunking into ≤1,000-text batches (`EMBED_BATCH_SIZE`).
- **O(n²) clustering in pure Python doesn't scale past toy examples.**
  3,543 names means ~6.3 million pairwise comparisons — tens of minutes at
  pure-Python speeds, not the instant results the small test cases showed.
  Rewrote the comparison as one vectorized NumPy matrix operation
  (normalize once, then a single matrix multiply for the full similarity
  matrix) instead of a nested loop. Verified correctness first against the
  known small-scale test case (identical clustering output), then
  confirmed against the real 3,543-name Zahav data before calling it done.

## A Postgres performance lesson, found by watching a query run for 30 minutes

Shrinking the hosted dataset down from nationwide (4.7M reviews) to a
curated subset meant removing the majority of a multi-million-row table.
The first attempt used `DELETE ... WHERE business_id IN (...)` — after
confirming via `EXPLAIN` that the query plan itself was reasonable (a
standard hash join, not a pathological nested loop), it was still killed
after 30+ minutes of genuine, non-stuck progress (verified through
`pg_stat_activity`, not just assumed). The issue wasn't the plan — `DELETE`
pays a WAL-logging and index-maintenance cost proportional to rows
*removed*, which is the wrong cost model when removing a majority of a
large table.

Rewrote it as `CREATE TABLE ... AS SELECT` (write only the rows being
*kept*) followed by a table swap — a cost model proportional to rows
*kept* instead. The same operation that didn't finish in 30 minutes
completed in under 10 seconds. Getting the swap correct took two more real
bugs found in testing: `DROP TABLE` silently drops a `SERIAL` column's
owned sequence, breaking future inserts until it's recreated explicitly;
and a handful of `dish_extractions` rows turned out to be orphaned
leftovers from the very first phase-1 test data, which would have violated
a foreign key constraint on re-creation if not cleaned up first.

## Protecting cached work from automated cleanup

A sub-3.0-star quality filter, meant to drop restaurants with little
chance of a genuinely good dish recommendation, was tested against the
live dataset before running it — and it would have silently deleted real,
already-extracted data for two restaurants used in actual testing
(including one demoed earlier in this project's own development), because
both happen to carry a 2.5-star Yelp rating. Fixed by exempting any
business with existing `dish_extractions` from rating-based pruning
regardless of its score — the same principle applied everywhere else in
the project (never discard work that's already been paid for), just
caught here before it caused damage instead of after.

## Matching model and effort to the task, not using one setting everywhere

Two different LLM calls in the pipeline, sized deliberately differently:

- **Extraction** (per review, high volume — hundreds of thousands of
  calls at real-dataset scale) uses Claude Haiku: cheap and fast, and the
  task (pull structured dish/sentiment/tag data from one review) doesn't
  need a larger model's reasoning depth.
- **Recommendation blurbs** (per dish, at most 10 per restaurant lookup —
  low volume, and the actual customer-facing output) uses Claude Opus, and
  specifically with reduced reasoning effort rather than the model's
  default. Measured directly: default effort spent tokens on invisible
  "thinking" for a task that's really just templated writing from facts
  already provided; lowering it cut total output tokens by roughly 25%
  with no drop in blurb quality on direct comparison.

## Known limitations, stated rather than hidden

- Dish categorization (the filter chips) is deterministic keyword matching
  against a small, hand-picked list — verified against real data to miss
  real cases (an Indian restaurant showed zero "Spicy" matches because the
  specific tags kept for its dishes never happened to include spicy-coded
  language). A documented, not-yet-built v2 is an LLM classification pass
  per dish, which is cheap at this scale (dishes are already deduplicated
  — dozens to low hundreds per restaurant, not thousands).
- The "reward good-but-rarely-ordered dishes" ranking problem above is
  unsolved by design, not by oversight.
- `dishes.embedding` (populated during aggregation, intended for future
  cross-restaurant semantic search — "best pad thai nearby") is currently
  write-only: computed and stored, never read back. Deliberately deferred
  rather than built speculatively, since it wasn't clear it would earn its
  cost at the current data volume.
- No automated test suite exists. Every fix in this document was verified
  by hand against real data and real failure modes during development —
  thoroughly, but not in a form a reviewer could re-run.
