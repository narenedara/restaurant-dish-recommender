# Deploying for friends to try

**Status: live.** Deployed at a Railway URL (get it with `railway domain`,
or see your Railway dashboard) backed by Supabase Postgres. This doc is both
the "how to do it again" instructions and a record of two non-obvious issues
hit during the real deployment — both are the kind of thing that silently
breaks a from-scratch attempt if you don't know to look for them.

This moves the app from your laptop to the internet: **Supabase** for Postgres
(pgvector built in, free tier), **Railway** for the FastAPI app itself. Total
cost at friends-trying-it-out scale: $0/month on free tiers, likely staying
there unless usage is heavy — see the note on Supabase's free-tier limits
below.

## 1. Supabase — the database

1. Go to [supabase.com](https://supabase.com), sign up, create a new project
   (pick any region close to you/your friends — doesn't need to match
   Railway's region, but closer is marginally faster).
2. Once it's provisioned, click **Connect** at the top of the project
   dashboard (not under Project Settings — Supabase moved this at some
   point) and copy the **connection string** (URI format, "Session pooler"
   or "Transaction pooler" tab — either works for this app's
   connection-per-request pattern). It looks like:
   ```
   postgresql://postgres.xxxxx:[PASSWORD]@aws-0-xxxxx.pooler.supabase.com:5432/postgres
   ```
   Replace `[PASSWORD]` with the database password you set during project
   creation — note that's a literal substitution (delete the brackets too),
   not a placeholder to fill in around.
3. pgvector is already available on Supabase — `db/schema.sql`'s
   `CREATE EXTENSION IF NOT EXISTS vector;` will enable it when you run the
   schema below, no extra step needed.
4. **Non-obvious issue #1: `search_path` comes back empty on a fresh Supabase
   project.** Postgres normally defaults new connections to search the
   `public` schema automatically; Supabase's pooler doesn't. Without a fix,
   every unqualified query this app makes (`SELECT ... FROM businesses`,
   which is all of them) fails with `relation "businesses" does not exist`
   — even though the tables exist exactly where they should, confirmed with
   `\dt public.*`. `ALTER DATABASE postgres SET search_path TO public;`
   looks like the fix but doesn't actually stick (the pooler resets session
   state). What does work: append a connection-string parameter so *every*
   connection sets it explicitly, confirmed through both raw `psql` and the
   app's actual `psycopg` client:
   ```
   postgresql://postgres.xxxxx:[PASSWORD]@aws-0-xxxxx.pooler.supabase.com:5432/postgres?options=-c%20search_path%3Dpublic
   ```
   Use *this* full string (with the `?options=...` suffix) as `DATABASE_URL`
   everywhere below, not the bare one Supabase's Connect panel gives you.

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

**Option B — CLI, deploy from your local directory directly** (what was
actually used):
```bash
railway login                                  # opens a browser to authenticate
railway init --name restaurant-dish-recommender  # creates the project
railway up -y -d                                # first deploy — creates the service
```

Then set the three secrets (`--stdin` keeps them out of shell history):

```bash
echo -n '<DATABASE_URL from step 1, with the ?options=... suffix>' | railway variable set DATABASE_URL --stdin --skip-deploys
```

**Non-obvious issue #2: don't extract secrets out of `.env` with `cut`/`grep`.**
This project's `.env` wraps values in literal quotes
(`ANTHROPIC_API_KEY='sk-ant-...'`), which `python-dotenv` strips automatically
— so the app has always worked locally despite that. A raw shell `cut -d= -f2-`
does *not* strip those quotes, so the key Railway receives ends up with the
quote characters embedded as part of the string: `'sk-ant-...'` instead of
`sk-ant-...`. That's invalid and fails with `AuthenticationError` on every
Claude call — not at deploy time, only when a request actually tries to use
it, which makes it easy to miss until you test a real cold-start lookup (a
cached restaurant's recommendations look completely normal, since they never
touch Claude). Extract correctly with Python's own parser instead of shell
text tools:

```bash
python3 -c "from dotenv import dotenv_values; import sys; sys.stdout.write(dotenv_values('.env')['ANTHROPIC_API_KEY'])" \
    | railway variable set ANTHROPIC_API_KEY --stdin --skip-deploys
python3 -c "from dotenv import dotenv_values; import sys; sys.stdout.write(dotenv_values('.env')['VOYAGE_API_KEY'])" \
    | railway variable set VOYAGE_API_KEY --stdin --skip-deploys
```

Railway reads the `Procfile` in this repo (`web: uvicorn api.main:app --host
0.0.0.0 --port $PORT`) to know how to start the app — no other config
needed. After setting the variables, redeploy so the app picks them up:

```bash
railway up -y -d
railway domain   # generates/shows the public URL
```

That URL (`https://your-app.up.railway.app` or similar) is what you send
your friends.

**Railway cost**: the free trial includes a one-time credit; after that it's
usage-based billing (CPU/memory/time actually consumed), typically a few
dollars a month for an app this size sitting mostly idle between friend
visits — not a recurring flat fee, and nothing is charged beyond the trial
without adding a payment method.

## 3. Sanity-check before sharing the link

A cached-restaurant check isn't enough — it never touches Claude or Voyage,
so it can't catch an API key problem (this is exactly how issue #2 above
almost shipped unnoticed). Check both:

```bash
# Cached restaurant — tests DATABASE_URL / search_path
curl -s https://your-app.up.railway.app/api/restaurants/search?name=Zahav

# A restaurant that's never been looked up — tests ANTHROPIC_API_KEY and
# VOYAGE_API_KEY too, since this forces a real extraction. Find one with:
#   SELECT id, name FROM businesses b WHERE NOT EXISTS
#   (SELECT 1 FROM reviews r WHERE r.business_id = b.id AND r.extracted_at IS NOT NULL) LIMIT 1;
curl -s https://your-app.up.railway.app/api/restaurants/<fresh-business-id>/recommendations?top_n=3
# then confirm it actually wrote data, not just returned 200:
#   SELECT count(*) FROM dish_extractions WHERE business_id = <fresh-business-id>;
```

Then open the URL in a browser and try a real search end to end before
sending it to anyone.

## What doesn't change

Nothing in the application code needs to know it's deployed vs. local —
`api/main.py` already reads `DATABASE_URL` from the environment the same way
either way, and the Anthropic/Voyage SDKs already read their API keys from
the environment automatically. The only new file for deployment is the
`Procfile`.
