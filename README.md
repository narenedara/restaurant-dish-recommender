# restaurant-dish-recommender

**[Try it live →](https://restaurant-dish-recommender-production.up.railway.app)**

Search for a restaurant and get back its best dishes — not its star
rating, the actual dish, ranked by what reviewers said about it, with a
short "why order this" blurb generated from their real words.

Under the hood: Claude extracts structured dish mentions and sentiment
from unstructured review text, Voyage embeddings merge near-duplicate dish
names ("birria taco" vs. "birria tacos"), and a Bayesian-averaged score
ranks dishes by evidence-weighted sentiment rather than a raw average that
small sample sizes would otherwise game.

## Why this is more than a CRUD app

A few things that came out of actually running this against real data,
not just designing it on paper — see [`DESIGNDECISIONS.md`](DESIGNDECISIONS.md)
for the full writeups:

- **Cost-aware architecture.** Extraction only ever runs the first time a
  restaurant is looked up, cached in Postgres forever after. Precomputing
  the dataset upfront was priced out at $480–690 before a single user saw
  a result; the on-demand design makes the real cost $0 until someone
  actually asks.
- **A ranking bug found by testing against real output, not by
  inspection.** A naive positive-ratio sort let a dish with 3/3 positive
  mentions outrank one with 12/14 — fixed with an IMDB-style
  Bayesian-averaged score, including a second, subtler bug in the
  baseline it blends against.
- **Two scaling limits hit for real, at Zahav's 3,543 distinct dish
  names**: Voyage's 1,000-text request cap, and an O(n²) Python
  similarity loop that didn't finish in reasonable time — fixed with
  request chunking and a vectorized NumPy rewrite, each verified against
  the small test case before trusting it on the large one.
- **Hallucination guardrails on the LLM-generated blurbs**, verified by
  reconstructing a real prompt and confirming the model's output traced
  back to actual stored reviewer tags, not invented detail.
- **A Postgres lesson from watching a query run for 30 minutes**: `DELETE`
  on a majority-of-table removal was replaced with `CREATE TABLE AS
  SELECT` + swap, cutting a 30+ minute operation to under 10 seconds.

[`IMPLEMENTATION.md`](IMPLEMENTATION.md) walks the whole pipeline
file-by-file and function-by-function, from a user's keystroke to the
ranked response, including a traced real example end to end.

## How it's built

```
restaurant-dish-recommender/
├── db/schema.sql           Postgres + pgvector schema
├── pipeline/
│   ├── load_data.py        Loads Yelp business/review JSON into Postgres
│   ├── filter_dataset.py   Subsets the full Yelp dataset to one city
│   ├── extract.py          Claude call: one review -> structured dish mentions
│   ├── batch_extract.py    Concurrent extraction across many reviews
│   ├── aggregate.py        Voyage embeddings + clustering -> one profile per dish
│   ├── lookup_restaurant.py  On-demand extract-and-cache for one restaurant
│   ├── recommend.py        Bayesian-averaged ranking + blurb generation
│   └── categories.py       Deterministic mood/category filtering
├── api/main.py              FastAPI layer — thin HTTP wrapper, no duplicated logic
└── frontend/index.html      Predictive search UI, no build step
```

One process, one port: FastAPI serves the API and the static frontend
together, so there's no CORS configuration. The API layer contains no
business logic of its own — every endpoint calls the same pipeline
functions the CLI scripts use, so there's exactly one implementation of
extraction, aggregation, and ranking, not two.

**Stack:** Python, FastAPI, Postgres + pgvector, Claude (Haiku for
high-volume extraction, Opus at reduced effort for blurb generation),
Voyage embeddings, vanilla HTML/JS frontend. Deployed on Railway, database
on Supabase — see [`DEPLOY.md`](DEPLOY.md) for the real deployment notes.

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY and VOYAGE_API_KEY

docker compose up -d
docker compose exec -T db psql -U postgres -d dishfinder < db/schema.sql
python -m pipeline.load_data --businesses data/sample/business_sample.json --reviews data/sample/review_sample.json

uvicorn api.main:app --reload
```

Open `http://localhost:8000`, search one of the two sample restaurants,
and its dishes should load. To run against real Yelp data instead of the
bundled sample, see `pipeline/filter_dataset.py`'s module docstring.

## Known limitations

Stated rather than hidden — full detail in
[`DESIGNDECISIONS.md`](DESIGNDECISIONS.md#known-limitations-stated-rather-than-hidden):
a dish that's genuinely excellent but rarely ordered currently can't
outrank a popular, merely-good one; the category filter is deterministic
keyword matching with known coverage gaps; `dishes.embedding` is computed
for future cross-restaurant semantic search but not yet read back; there's
no automated test suite.

## Data

Built on the [Yelp Open Dataset](https://business.yelp.com/data/resources/open-dataset/),
used here for non-commercial, educational purposes.
