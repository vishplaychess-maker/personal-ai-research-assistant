---
name: context7
description: >
  Use Context7 to fetch up-to-date, version-accurate documentation for
  third-party libraries before writing or modifying code that uses them.
  Prefer Context7 over memory or web search when you need exact API
  signatures, parameter names, or current behavior of a dependency.
license: MIT
---

# Context7

Context7 serves current, versioned docs for thousands of open-source
libraries. Use it when you are about to write code against a library whose
API you are not 100% sure of — exact signatures beat memory.

## When to use
- Writing code that calls a third-party library (pip, npm, PyPI, crates...)
- Upgrading a dependency and unsure what changed in the new version
- Verifying an API signature, option name, or return shape before using it

## How to use
1. Resolve the library id: call the Context7 MCP tool `resolve-library-id`
   (or its search) to find the canonical library id + version.
2. Fetch docs: call `get-library-docs` with the resolved id and the topic or
   keywords you need, so you only pull the relevant API surface.
3. Implement against the fetched docs, and note the version you verified in
   your summary.

## Availability & fallback
- If the Context7 MCP tools are not configured in this environment, fall
  back in this order:
  1. The library's official documentation site (web search for it).
  2. Confirm the installed version (`pip show <pkg>` / `npm ls <pkg>`).
  3. Read the installed package's source or type stubs directly when the
     official docs are ambiguous.
- Never guess an API shape from memory when Context7 or a fallback is
  available — guessing is how hallucinated code ships.

## Rules
- One library = one docs lookup. Do not fetch a whole catalog.
- Keep the fetched surface in the response only as far as it supports the
  code you are writing.
