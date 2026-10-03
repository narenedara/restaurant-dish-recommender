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
app wakes it back up automatically within a few seconds). If you've loaded
the nationwide dataset, check your actual usage in Supabase's dashboard —
businesses + reviews alone may approach or exceed 500MB depending on how
much you've loaded; if so, either upgrade ($25/mo for the next tier) or trim
back to a smaller slice of cities before migrating.

## 2. Railway — the app

Railway can deploy straight from your local directory with its CLI — no
GitHub repo required (handy since this project isn't in git yet). From the
project root:

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
