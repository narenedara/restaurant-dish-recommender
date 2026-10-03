# Deploying for friends to try

This moves the app from your laptop to the internet: **Supabase** for Postgres
(pgvector built in, free tier), **Railway** for the FastAPI app itself. Total
cost at friends-trying-it-out scale: $0/month on free tiers, likely staying
there unless usage is heavy — see the note on Supabase's free-tier limits
below.

## 1. Supabase — the database

1. Go to [supabase.com](https://supabase.com), sign up, create a new project
   (pick any region close to you/your friends — doesn't need to match
   Railway's region, but closer is marginally faster).
2. Once it's provisioned, go to **Project Settings → Database** and copy the
   **connection string** (URI format, "Session pooler" or "Transaction
   pooler" — either works for this app's connection-per-request pattern).
   It looks like:
   ```
   postgresql://postgres.xxxxx:[PASSWORD]@aws-0-xxxxx.pooler.supabase.com:5432/postgres
   ```
   Replace `[PASSWORD]` with the database password you set during project
   creation.
3. pgvector is already available on Supabase — `db/schema.sql`'s
   `CREATE EXTENSION IF NOT EXISTS vector;` will enable it when you run the
   schema below, no extra step needed.

### Migrate your data (not a fresh load — preserve what's already cached)

You've already paid real money to extract and generate blurbs for several
restaurants (Zahav, Caribbean Palm, the Royal Indian Cuisines, etc.). A
`pg_dump` → restore carries all of that over, instead of re-extracting
everything from scratch on Supabase (which would mean paying for it twice).

```bash
# Dump your local database (schema + all data)
docker compose exec -T db pg_dump -U postgres -d dishfinder --no-owner --no-privileges > dishfinder_dump.sql

# Restore into Supabase — paste your Supabase connection string here
psql "postgresql://postgres.xxxxx:[PASSWORD]@aws-0-xxxxx.pooler.supabase.com:5432/postgres" < dishfinder_dump.sql
```

If that errors on the `vector` extension (some pooled connection strings
don't have permission to run `CREATE EXTENSION`), run this first against the
**direct** (non-pooler) connection string from the same Supabase settings
page, then retry the restore:

```bash
psql "<direct connection string>" -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**Supabase free tier limits worth knowing**: 500MB database storage, and the
project pauses after a week of no API/database activity (anyone visiting the
app wakes it back up automatically within a few seconds). The dataset as
currently trimmed (Philadelphia only, sub-3-star untouched restaurants
dropped, reviews capped at 40/business except already-extracted ones) is
**~190MB** — comfortable margin. If you expand the dataset later (more
cities, a higher review cap), re-check `SELECT
pg_size_pretty(pg_database_size('dishfinder'));` before migrating —
`pipeline/trim_dataset.py` has the tools to shrink it back down (uses a
create-filtered-copy-and-swap approach, not DELETE — much faster at this
scale, see that file's docstring).

## 2. Railway — the app

Code's already on GitHub: https://github.com/narenedara/restaurant-dish-recommender

**Option A — GitHub integration (recommended, auto-redeploys on push):**
1. [railway.app](https://railway.app) → New Project → **Deploy from GitHub repo**
2. Pick `restaurant-dish-recommender`, authorize Railway's GitHub App if asked
3. Railway reads the `Procfile` automatically — no build config needed

**Option B — CLI, deploy from your local directory directly:**
```bash
railway login          # opens a browser to authenticate
railway init            # creates a new Railway project, asks for a name
railway up              # uploads this directory and deploys it
```

Then set the three secrets it needs (**Railway dashboard → your service →
Variables**, or via CLI):

```bash
railway variables --set "DATABASE_URL=postgresql://postgres.xxxxx:[PASSWORD]@aws-0-xxxxx.pooler.supabase.com:5432/postgres"
railway variables --set "ANTHROPIC_API_KEY=sk-ant-..."
railway variables --set "VOYAGE_API_KEY=pa-..."
```

Railway reads the `Procfile` in this repo (`web: uvicorn api.main:app --host
0.0.0.0 --port $PORT`) to know how to start the app — no other config
needed. After setting the variables, redeploy so the app picks them up:

```bash
railway up
```

Railway will give you a public URL (`https://your-app.up.railway.app` or
similar, under **Settings → Networking → Generate Domain** if one isn't
assigned automatically) — that's the link to send your friends.

**Railway cost**: the free trial includes a one-time credit; after that it's
usage-based billing (CPU/memory/time actually consumed), typically a few
dollars a month for an app this size sitting mostly idle between friend
visits — not a recurring flat fee, and nothing is charged beyond the trial
without adding a payment method.

## 3. Sanity-check before sharing the link

```bash
curl -s https://your-app.up.railway.app/api/restaurants/search?name=Zahav
```

Should return the same JSON you'd get locally. Then open the URL in a
browser and try a real search end to end before sending it to anyone.

## What doesn't change

Nothing in the application code needs to know it's deployed vs. local —
`api/main.py` already reads `DATABASE_URL` from the environment the same way
either way, and the Anthropic/Voyage SDKs already read their API keys from
the environment automatically. The only new file for deployment is the
`Procfile`.
