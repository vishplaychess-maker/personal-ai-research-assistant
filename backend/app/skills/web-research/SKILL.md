---
name: web-research
description: Run autonomous web research on a topic. Use when the user asks to research or summarize an unfamiliar topic, compare facts across sources, or get a cited report with live sources (works without them giving a URL).
---

# Web Research Skill

When the user asks you to research or summarize a topic on the live web,
follow these steps to produce a grounded, cited answer.

## Steps

1. **Identify the core question.** Restate the user's topic as one focused
   research question.
2. **Use the web search + deep research tools.** The backend can search the
   web (DuckDuckGo) and scrape the top results. Let those results populate
   your context.
3. **Synthesize, don't copy.** Write the answer in your own words. Weave in
   facts from multiple sources rather than quoting one page.
4. **Cite every claim.** Every factual claim gets an inline source link
   `[Source](url)` so the user can verify. If a source is uncertain, say so.
5. **Flag gaps.** If the search returns nothing useful, tell the user what you
   could and could not verify instead of inventing facts.

## Rules

- Never fabricate a URL, statistic, or quote. Only cite sources that were
  actually retrieved.
- Prefer current sources over outdated ones when a topic is time-sensitive.
- Keep the final answer actionable and skimmable (use headings where helpful).
