# MP-4C — dedicated Python executor service image.
#
# Boundary: only THIS service reaches the Docker daemon, over a bind-mounted unix
# socket. The backend, the frontend and every executed Python container receive no
# socket and no daemon credentials.
#
# Base is the SAME digest-pinned runtime the code containers use, so the executor
# image and the code containers cannot drift. It is already provisioned locally;
# building does not re-tag or pull it at service STARTUP.
FROM python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b

# The service shells out to the `docker` CLI; the daemon itself is reached over
# the mounted socket. `docker-cli` provides ONLY the client (no daemon).
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Same HTTP stack versions the application pins, so behaviour matches the tests.
RUN pip install --no-cache-dir "fastapi==0.115.0" "uvicorn==0.30.6" "pydantic==2.9.0"

WORKDIR /srv

# ONLY what readiness + execution need. The rest of tests/ is deliberately not
# shipped, so no application code and no test fakes are present in this image.
COPY executor_service/ /srv/executor_service/
# The SHARED contract (same file the backend ships), so both sides use ONE
# definition of the signed grant.
COPY backend/mp4c_contract /srv/mp4c_contract/
COPY tests/__init__.py /srv/tests/__init__.py
COPY tests/smoke/__init__.py /srv/tests/smoke/__init__.py
COPY tests/smoke/mp4c_isolation_probe.py /srv/tests/smoke/mp4c_isolation_probe.py
COPY tests/smoke/mp4c_python_image.py /srv/tests/smoke/mp4c_python_image.py
COPY tests/smoke/mp4c_executor_service.py /srv/tests/smoke/mp4c_executor_service.py

# The durable replay guard lives on a NAMED VOLUME. Creating the directory here
# (owned by the service user) makes a FRESH volume inherit that ownership, so the
# non-root process can write it.
RUN mkdir -p /var/lib/executor && chown 10001:0 /var/lib/executor

ENV PYTHONPATH=/srv \
    PYTHONDONTWRITEBYTECODE=1 \
    EXECUTOR_SERVICE_PORT=8091 \
    EXECUTOR_SERVICE_NONCE_DB=/var/lib/executor/nonces.db

# uid 10001 with GID 0: the mounted daemon socket is root:root 0660, so group 0 is
# required to use it, while the process itself stays NON-root.
USER 10001:0

EXPOSE 8091
CMD ["python", "-m", "executor_service"]
