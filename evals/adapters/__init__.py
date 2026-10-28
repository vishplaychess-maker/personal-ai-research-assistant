"""Product-path adapters for the eval harness.

These are lazy-imported only when `--inprocess` / `--backend` is selected, so
the default `--offline` Gate A stays hermetic (no `backend/app` import).
"""
