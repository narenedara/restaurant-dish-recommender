# restaurant-dish-recommender

Starter scaffold for the dish-recommendation project. Phase 1 built a
database, a data loader, and a single-review extraction script. Phase 2
added batch/on-demand extraction and aggregation — rolling many reviews'
worth of extractions into one dish profile per business — plus ranking
and recommendation blurbs. Phase 3 (below) puts a browser UI on top of
all of that via a FastAPI layer. No LangGraph — the UX this project
settled on (search a restaurant, see ranked dishes) doesn't need
multi-turn agent orchestration; the existing pipeline functions already
are a clean, deterministic chain, and FastAPI just calls them directly.

## What's here

```
restaurant-dish-recommender/
├── db/
│   └── schema.sql          Postgres tables (businesses, reviews, extractions, dishes)
├── pipeline/
│   ├── load_data.py        Loads business/review JSON into Postgres
│   ├── filter_dataset.py   Subsets the full Yelp dataset down to one city
│   ├── extract.py          Calls Claude to pull dish mentions out of one review
│   ├── batch_extract.py    Runs extraction over many reviews, writes dish_extractions
│   ├── aggregate.py        Rolls dish_extractions into per-business dish profiles
│   ├── lookup_restaurant.py  On-demand: extract + aggregate one restaurant, on request
│   └── recommend.py        Ranks a restaurant's dishes, writes a "why order this" blurb for the top N
├── api/
│   └── main.py             FastAPI layer — thin HTTP wrapper around the pipeline functions above
├── frontend/
│   └── index.html          Search UI (plain HTML/JS, no build step) that talks to api/main.py
├── data/sample/            Tiny fake dataset so you can run everything today
├── docker-compose.yml      Local Postgres + pgvector, one command to start
├── requirements.txt
└── .env.example
```

## Prerequisites

- Python 3.11+
- Docker Desktop (or any Docker engine) — this runs your local database, you
  don't install Postgres directly
- An Anthropic API key (console.anthropic.com)

## Setup — run these in order

**1. Virtual environment.** This keeps this project's Python packages
separate from everything else on your machine, so installing something here
never breaks another project.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Secrets.** Copy the example env file and fill in your real API key.
`.env` is in `.gitignore` on purpose — never commit real credentials.

```bash
cp .env.example .env
# then edit .env and paste in your ANTHROPIC_API_KEY
```

**3. Start the database.**

```bash
docker compose up -d
```

This pulls a Postgres image with the `pgvector` extension pre-installed and
starts it in the background, listening on `localhost:5432`. `docker compose
ps` shows you it's running; `docker compose logs -f db` shows you the logs.

**4. Create the tables.**

```bash
docker compose exec -T db psql -U postgres -d dishfinder < db/schema.sql
```

**5. Load the sample data** (two fake restaurants, a handful of fake
reviews — enough to prove the pipeline works before you touch the real
dataset):

```bash
python -m pipeline.load_data --businesses data/sample/business_sample.json --reviews data/sample/review_sample.json
```

**6. Run extraction on one review.** This is the actual "AI" part — it
sends one review's text to Claude and gets back structured dish data.

```bash
# quickest: test it on raw text, no database needed
python -m pipeline.extract --text "The birria tacos here are incredible, super rich broth. Their horchata is way too sweet though."

# or: pull a real row out of the database you just loaded
python -m pipeline.extract --review-id 1
```

You should see parsed JSON like:

```json
[
  {"dish": "birria tacos", "sentiment": "positive", "tags": ["rich", "signature"]},
  {"dish": "horchata", "sentiment": "negative", "tags": ["too sweet"]}
]
```

If that works, phase 1 is done — you have a working slice of the whole
system: data in Postgres, and a script that turns review text into
structured dish data via Claude. Everything from here (LangGraph, batch
processing, the API, the frontend) builds on top of this.

## Getting the real dataset

Once the sample data works, replace it with the actual [Yelp Open
Dataset](https://business.yelp.com/data/resources/open-dataset/) (free,
requires accepting Yelp's terms, ~10GB uncompressed). It ships as
newline-delimited JSON, same shape as the sample files here, so
`load_data.py` works on it unchanged — just point `--businesses` /
`--reviews` at the real files instead.

Don't load the whole thing on your first real run — it's millions of
businesses and reviews. Filter to one city first with
`pipeline/filter_dataset.py`, which streams through the raw files (same
line-by-line approach as `load_data.py`, for the same memory reason) and
writes out just the matching businesses and their reviews:

```bash
python -m pipeline.filter_dataset \
    --businesses /path/to/yelp_academic_dataset_business.json \
    --reviews /path/to/yelp_academic_dataset_review.json \
    --city Philadelphia --state PA \
    --out-dir data/philadelphia
```

By default it also drops any business without "Restaurants" in its
category list — pass `--category ""` to keep every business type in the
city instead. Then load the filtered files exactly like the sample data:

```bash
python -m pipeline.load_data \
    --businesses data/philadelphia/business.json \
    --reviews data/philadelphia/review.json
```

## Phase 2 — extraction and aggregation

Phase 1's `extract.py` proves the Claude call works on one review and only
prints the result — nothing gets saved. Phase 2 adds the pieces that turn
that into a real, persisted pipeline: extraction at scale, aggregation of
raw per-review results into one dish profile per business, and — the part
that actually matters for cost — a choice about **when** that work happens.

**0. If your database already exists from phase 1**, add the new tracking
column (a fresh `db/schema.sql` run already includes it):

```bash
docker compose exec -T db psql -U postgres -d dishfinder \
    -c "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMPTZ;"
```

**1. Get a Voyage API key** (free tier at
[dash.voyageai.com](https://dash.voyageai.com)) and add it to `.env` as
`VOYAGE_API_KEY`. Aggregation uses it to embed dish names.

### Two ways to run extraction: on-demand vs. upfront

Extracting dishes from *every* review in the dataset upfront only makes
sense if the product needs to answer city-wide questions ("best pad thai
in Philly") — that requires dish data from restaurants across the city
whether or not anyone's asked about them yet. If the product is instead a
**lookup** — "type a restaurant, see its dishes" — extracting restaurants
nobody ever searches for is pure waste. Pick based on which one you're
building:

**Lookup (recommended to start with):** `pipeline/lookup_restaurant.py`
extracts and aggregates exactly one restaurant, at the moment someone
looks it up — no upfront cost, first lookup takes a few seconds, every
lookup after that is a free cache read (same `extracted_at` tracking, so
a review is never re-extracted):

```bash
python -m pipeline.lookup_restaurant --name "Zahav"
```

If the name matches more than one restaurant, it lists them numbered
(with address, so same-named places are easy to tell apart) and prompts
you to pick one — or pass `--business-id` directly if you already know
which one you want.

`lookup_restaurant.py`'s output is every dish, unranked, sorted only by
mention count — useful for inspecting the raw data, but not a
recommendation. For an actual "what should I order" answer, use
`pipeline/recommend.py` instead — same lookup/extraction underneath, but
it ranks the dishes and writes a short blurb for the best ones:

```bash
python -m pipeline.recommend --name "Zahav"
python -m pipeline.recommend --business-id 34 --top-n 5       # 1-10, default 3
python -m pipeline.recommend --business-id 34 --min-mentions 5  # require more signal before ranking a dish
```

Ranking is deterministic: a dish needs `--min-mentions` (default 3) total
mentions before it's ranked at all, then eligible dishes are sorted by
positive-sentiment ratio (ties broken by mention count). This deliberately
leaves out low-mention dishes rather than guessing at them — a dish
ordered twice might be great, but two data points can't establish that
reliably. Surfacing good-but-rarely-ordered dishes fairly is a real
problem worth solving later (see `recommend.py`'s docstring); v1 just
excludes them rather than ranking them unreliably. The blurb for each
recommended dish comes from a Claude Opus call grounded in that dish's
real tags and sentiment counts — general knowledge about the dish is
allowed in to add context, but never to override what reviewers actually
said.

**Discovery / precompute everything:** `batch_extract.py` +
`aggregate.py` process the whole dataset (or a whole business) upfront.
Real cost at real scale — check current Claude Haiku pricing on
[console.anthropic.com](https://console.anthropic.com) and estimate before
running this over more than a test batch:

```bash
python -m pipeline.batch_extract --all --limit 10                                     # test on a handful first
python -m pipeline.batch_extract --all --max-reviews-per-business 150 --concurrency 20  # then the rest, in parallel
python -m pipeline.aggregate --all
```

`--concurrency` (default 10) controls how many reviews are sent to Claude
at once — each is a separate API round-trip, so running them one at a
time doesn't scale past a few hundred reviews. Turn it down if you hit
rate limits, up if you have headroom. Aggregation groups `dish_extractions`
into one row per distinct dish per business — merging near-duplicate names
("birria taco" vs "birria tacos") via exact matching first, then Voyage
embedding similarity for what exact matching misses.

**`--max-reviews-per-business` matters a lot at real-city scale.** A
handful of restaurants can have thousands of reviews each while the
typical one has a few dozen — Philadelphia's data has restaurants ranging
from ~10 reviews to Reading Terminal Market's 5,778. Capping keeps, per
business, only the longest pending reviews (ranked by character length,
not chronology), which is a free, already-available proxy for exactly
what you'd want: measured on this project's own extracted data, reviews
under 200 characters mention zero dishes over a third of the time, while
reviews over 1,800 characters do less than 2% of the time, and average
dish mentions per review rises roughly 5x across that range. A cap of 150
only affects restaurants that actually have more than that (~20% of
Philadelphia's, in practice) and cuts total reviews processed by roughly
47% — see `fetch_pending_reviews`'s docstring in `batch_extract.py` for
the full reasoning and why a hard "skip short reviews" filter *isn't*
used instead (there's no length short enough to be reliably safe to skip
outright — even 25-50 character reviews still mention a dish about a
third of the time). Not applied by default, and not something
`pipeline.lookup_restaurant`'s on-demand single-restaurant lookups use —
a restaurant someone specifically searched for gets full coverage
regardless of size.

```bash
docker compose exec -T db psql -U postgres -d dishfinder \
    -c "SELECT name, mention_count, positive_count, negative_count, tags FROM dishes ORDER BY mention_count DESC;"
```

You should see one row per dish, with mentions merged across every review
that named it. Re-running `batch_extract` picks up only new reviews;
re-running `aggregate` fully recomputes each business's dish profiles from
its `dish_extractions` rows (see that script's docstring for why it's
delete-and-reinsert rather than an incremental update).

If that works with the sample data, swap in a real city's worth of Yelp
data (see above) and run both scripts again — that's the real test, since
a single city will have far more near-duplicate dish names for the
embedding-clustering step to sort out than the tiny sample dataset does.

## Phase 3 — a browser UI

Everything so far has been a terminal command. Phase 3 wraps
`pipeline.lookup_restaurant` and `pipeline.recommend`'s functions in a
FastAPI server (`api/main.py`) and a plain HTML/JS page (`frontend/`), so
you can search a restaurant and see its top dishes in a browser instead.
`api/main.py` adds no new logic — every endpoint calls the same
`find_businesses` / `get_dish_profiles` / `rank_dishes` / `generate_blurb`
functions the CLI scripts already use and that phase 2 tested end-to-end.

Run it:

```bash
uvicorn api.main:app --reload
```

Then open http://localhost:8000. Type a restaurant name, pick one if more
than one matches, and its ranked dishes load the same way `recommend.py`
would print them — instantly if it's been looked up before, taking a
while on a genuine first lookup (it's extracting every review with Claude
in the background; the page just shows a loading state while that runs).

No LangGraph here: the "search a restaurant, see ranked dishes" UX is a
straight-line lookup, not a multi-turn conversation, so there's no agent
decision-making or cross-turn memory to justify an orchestration
framework. If this ever grows into a chat-style assistant (follow-up
questions like "anything vegetarian?", remembering what restaurant you're
already talking about across turns), that's exactly the shape where
LangGraph would earn its place — revisit then, not before.

## A few things worth understanding, not just copying

- **Why Docker for the database instead of installing Postgres locally?**
  Docker gives you a clean, disposable, identical-everywhere database. If
  you mess up the schema, `docker compose down -v && docker compose up -d`
  wipes it and starts fresh in seconds — no uninstalling anything.
- **Why a `.env` file instead of hardcoding the API key?** Secrets never
  belong in code, because code ends up in git, and git ends up on GitHub.
  `.env` + `.gitignore` is the standard pattern for keeping secrets out of
  version control while still making them available to your scripts.
- **Why `client.messages.parse()` with a Pydantic model instead of asking
  Claude to "please return JSON"?** Prompting for JSON can still come back
  malformed (markdown fences, a trailing comment, a missing field).
  Structured outputs constrain the model's decoding so the response is
  *guaranteed* to match your schema — no parsing failures, no retry logic
  needed for malformed JSON.
- **Why does `load_data.py` read the file line by line instead of
  `json.load()`-ing the whole thing?** The real dataset's review file is
  several GB. Loading it all into memory at once will exhaust your RAM.
  Reading one line at a time (the file is newline-delimited JSON, one
  object per line) keeps memory usage constant no matter how big the file
  gets — this pattern is worth knowing generally, not just here.
- **Why two passes for dish-name matching instead of just embedding
  everything?** Exact-match grouping first is nearly free (no API call)
  and handles the bulk of duplicates, since the same dish gets typed
  identically far more often than not. Only the leftover *distinct*
  strings get embedded and clustered, which keeps the embedding call
  count proportional to how much real variation there is, not to how many
  reviews you have.
- **Why does `aggregate.py` delete and re-insert a business's `dishes`
  rows instead of updating them in place?** Embedding-based clustering can
  draw slightly different group boundaries each time new extractions get
  added — a name that clustered alone last run might join a bigger group
  this run. Trying to reconcile that against previously-written rows
  invites duplicate or stale profiles; recomputing from `dish_extractions`
  (the source of truth) every time is simpler and always self-consistent.

## Next steps

- **Fix the "recommends a disliked dish" edge case**: `rank_dishes()`
  currently ranks whatever clears `--min-mentions`, with no floor on
  sentiment itself — a restaurant with few eligible dishes can surface one
  that's net-negative just to fill out `--top-n`. Worth requiring a
  minimum positive ratio (e.g. > 0.5) to be recommended at all, separate
  from the mention-count eligibility bar.
- **Discovery mode** ("best pad thai in Philly"): needs the precompute
  path (`batch_extract`/`aggregate --all`) run across a real slice of the
  city, plus a way to search across businesses by dish rather than by
  name — the `dishes.embedding` column is sitting there unused for exactly
  this.
- **A conversational UX**: if this grows past single-lookup search into
  multi-turn follow-ups, that's the point where LangGraph is worth
  adopting — not before.
