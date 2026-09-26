"""MP-4C — digest-pinned Python runtime image (PROMOTED contract).

The authoritative image pin for the dedicated executor service. Nothing here
pulls: `readiness()` fails closed while the exact pinned image is absent locally,
so no code path can mistake "the tag resolves" for "the runtime works".

Verified from the live registry (read-only `docker manifest inspect`):
  tag  python:3.12-slim  ->  3.12.14-slim-trixie  (application/vnd.oci.image.index)
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable, Optional, Tuple

#: The repository's production Python (backend/Dockerfile: python:3.12-slim).
IMAGE_REF = "python:3.12-slim"
IMAGE_VERSION = "3.12.14-slim-trixie"
PLATFORM = "linux/amd64"
#: Multi-arch INDEX digest for the tag (what the tag resolved to at pin time).
IMAGE_INDEX_DIGEST = "sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
#: The linux/amd64 IMAGE MANIFEST digest — what we actually pin the executor to.
IMAGE_AMD64_DIGEST = "sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b"
#: Digest-pinned, single-platform reference the executor must run.
PINNED_REF = IMAGE_REF + "@" + IMAGE_AMD64_DIGEST

#: Exact, reviewable provisioning procedure. It PULLS an image, so it is never
#: run by a test and never run at service startup or on a request.
PROVISION_COMMANDS = (
    "# 0. resolve + record the digests (read-only, no pull)",
    "docker buildx imagetools inspect %s" % IMAGE_REF,
    "# 1. AUTHORIZED pull of the digest-pinned image only",
    "docker pull %s" % PINNED_REF,
    "# 2. verify the local image is EXACTLY the pinned digest",
    "docker image inspect %s --format '{{.Id}} {{json .RepoDigests}}'" % PINNED_REF,
    "# 3. run the adversarial probe against the Python image",
    "python tests/smoke/mp4c_isolation_probe.py",
)


def _docker(args, *, timeout=60):
    p = subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def local_repo_digests(ref: str = PINNED_REF) -> Optional[list]:
    """RepoDigests of the locally present pinned image, or None when absent."""
    rc, out, _ = _docker(["image", "inspect", ref, "--format", "{{json .RepoDigests}}"])
    if rc != 0:
        return None
    try:
        return list(json.loads(out.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        return None


def readiness(*, expected_digest: str = IMAGE_AMD64_DIGEST,
              ref: str = PINNED_REF) -> Tuple[bool, Optional[str]]:
    """(ok, reason). FAIL CLOSED: absent -> not_provisioned; wrong -> mismatch."""
    digests = local_repo_digests(ref)
    if digests is None:
        return False, "python_image_not_provisioned"
    if not any(d.endswith(expected_digest) for d in digests):
        return False, "python_image_digest_mismatch"
    return True, None


def pinned_reference_is_digest_form() -> bool:
    """A pin must be immutable: `name:tag@sha256:<64 hex>`, never a bare tag."""
    if "@sha256:" not in PINNED_REF:
        return False
    tail = PINNED_REF.rsplit("@sha256:", 1)[1]
    return len(tail) == 64 and all(c in "0123456789abcdef" for c in tail)
