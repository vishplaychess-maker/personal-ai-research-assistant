"""MP-4C — the DEDICATED, separately-runnable Python execution service.

This package is deliberately OUTSIDE `app/**`: nothing in the application imports
it, and it is never registered on the application's FastAPI instance. It is the
only component that talks to the Docker daemon. The application never receives a
Docker socket or daemon credentials, and neither does any executed container.

Modules
-------
`image`     digest-pinned runtime image + local inspection (never pulls)
`contract`  the fixed hardened argv + stdin-only source delivery
`quota`     atomic single-instance caps (1 active/user, 2 globally)
`runner`    one container per request, bounded, always cleaned up
`readiness` fail-closed startup gate (daemon + image + full isolation probe)
`identity`  token -> identity registry (a caller cannot choose its identity)
`service`   the orchestration boundary
`app`       the narrowly authenticated internal HTTP surface

See `README.md` for the deployment posture: this is runnable locally, and it is
NOT deployable under the repository's current container topology.
"""

__all__ = []
