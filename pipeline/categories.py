"""
Deterministic dish-category filtering ("Spicy", "Hearty", etc.) — no new
API calls, no schema change. dishes.tags is open-vocabulary (whatever
language extract.py happened to pull from real review text — "fiery",
"kick of heat", and "spicy" could all mean the same thing but are three
different strings), while a filter UI needs a small, fixed set of
categories. This module bridges that gap with curated keyword lists
matched against tags already sitting in Postgres.

This is a v1 choice, not a final one: it only catches what the original
extraction already phrased in a matchable way, and pipeline.aggregate
only keeps each dish's top 5 most common tags (MAX_TAGS), so a genuinely
spicy dish could be missed if spicy-adjacent language didn't make that
cut, or if reviewers phrased it in a way a keyword list doesn't
anticipate. It also doesn't detect negation ("not spicy enough" still
matches "Spicy") — acceptable here because tags are short extracted
phrases ("too sweet", "bland"), not full sentences, so negated phrasing
is rare in practice. If keyword coverage turns out too thin once this is
used against real data, the natural v2 is an LLM classification pass per
dish (cheap — dishes are already deduplicated, so a restaurant has dozens
to low-hundreds, not thousands) instead of more keyword tuning.

Usage:
    categorize_dish(dish)                      -> ["spicy", "hearty"]
    available_categories(profiles)             -> {"spicy": 4, "hearty": 2, ...}
    filter_by_categories(profiles, ["spicy"])  -> [profiles matching any selected category]
"""

# Matches the top_n cap used everywhere a filtered list actually gets
# rendered (api/main.py's `le=10`, recommend.py's CLI `choices=range(1,
# 11)`, the frontend's hardcoded top_n=10) — not an independent limit.
# available_categories() reports counts capped at this, so a chip never
# promises more matches than a user filtering on it will ever actually
# see (a restaurant can have 42 "sweet" dishes; showing "42" on the chip
# and then only ever surfacing 10 of them reads as broken, not generous).
MAX_CATEGORY_DISPLAY = 10

# Keyword lists are intentionally short and literal — matched as
# case-insensitive substrings against each dish's tags, so a keyword like
# "sweet" also catches a tag like "too sweet" or "overly sweetened".
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "spicy": ["spicy", "fiery", "heat", "kick", "chili", "chile", "jalape", "habanero", "scotch bonnet", "cayenne"],
    "hearty": ["hearty", "filling", "generous portion", "heavy", "substantial", "large portion", "big portion"],
    "light": ["light", "fresh", "refreshing", "crisp", "delicate"],
    "sweet": ["sweet", "sugary", "dessert", "syrupy", "honey"],
    "comfort": ["comforting", "classic", "familiar", "homestyle", "nostalgic", "traditional", "old-school"],
}


def categorize_dish(dish: dict) -> list[str]:
    """Which of CATEGORY_KEYWORDS this dish's tags match. A dish can match
    more than one category (e.g. a dish tagged "spicy" and "filling")."""
    tags_lower = [t.lower() for t in dish.get("tags", [])]
    return [
        category
        for category, keywords in CATEGORY_KEYWORDS.items()
        if any(keyword in tag for tag in tags_lower for keyword in keywords)
    ]


def available_categories(profiles: list[dict]) -> dict[str, int]:
    """Category -> count of matching dishes, restricted to categories with
    at least one match — so a filter UI never offers a dead-end chip with
    zero results for this specific restaurant. Counts are capped at
    MAX_CATEGORY_DISPLAY: the real number of matches can be much larger
    (a popular restaurant might have 42 dishes tagged "sweet"), but only
    MAX_CATEGORY_DISPLAY of them are ever actually shown once someone
    filters, so reporting the true count would overpromise."""
    counts: dict[str, int] = {}
    for dish in profiles:
        for category in categorize_dish(dish):
            if counts.get(category, 0) < MAX_CATEGORY_DISPLAY:
                counts[category] = counts.get(category, 0) + 1
    return counts


def filter_by_categories(profiles: list[dict], selected: list[str]) -> list[dict]:
    """Dishes matching *any* of the selected categories (OR, not AND) —
    requiring all selected categories at once would frequently return
    nothing, especially for smaller restaurants with fewer distinct dishes."""
    if not selected:
        return profiles
    selected_set = {c.lower() for c in selected}
    return [p for p in profiles if selected_set & set(categorize_dish(p))]
