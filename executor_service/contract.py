"""MP-4C — the FIXED Docker execution contract (PROMOTED, authoritative).

ONE source of truth for how a container is launched. Every option is fixed here:
the caller CANNOT supply an image, an argv, a mount, a network mode, an
environment variable or a resource limit.

Python source is NEVER a command-metadata element: the fixed command ends in
`python -I -B -u -` and the bounded UTF-8 code is written to the container's
stdin (Docker `--interactive`). No shell, no volume, no environment variable and
no temporary host file carries the code.
"""
from __future__ import annotations

from dataclasses import dataclass

from executor_service.image import PINNED_REF

#: Fixed trailing command. `-` reads the program from STDIN.
DOCKER_STDIN_FLAG = "--interactive"
PYTHON_ARGV = ("python", "-I", "-B", "-u", "-")

#: Ownership marker. A STABLE id (not a per-process random) so that after a
#: restart this service can recognise, and only remove, containers it owns.
OWNER_LABEL = "mp4c.owner"
DEFAULT_OWNER = "mp4c-executor"
CONTAINER_PREFIX = "mp4c_exec_"

MAX_CODE_BYTES = 8192


@dataclass(frozen=True)
class ExecutionPlan:
    """Everything a runner needs. `stdin_code` is the ONLY carrier of source.

    `__repr__` deliberately withholds the payload so a plan can never leak source
    into logs, tracebacks, or a `repr()` of the service's own records.
    """

    argv: tuple
    stdin_code: bytes
    name: str
    owner: str

    def __repr__(self) -> str:
        return ("ExecutionPlan(argv=%r, name=%r, owner=%r, stdin_code=<%d bytes withheld>)"
                % (list(self.argv), self.name, self.owner, len(self.stdin_code)))


def build_argv(*, name: str, owner: str = DEFAULT_OWNER) -> list:
    """The FIXED hardened argv. NO code — only the name and owner label vary."""
    return [
        "docker", "run", "--rm", "--name", name,
        "--label", "%s=%s" % (OWNER_LABEL, owner),
        DOCKER_STDIN_FLAG,
        "--network", "none",
        "--read-only",
        "--tmpfs", "/scratch:rw,noexec,nosuid,nodev,size=16m",
        "--user", "65534:65534",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "64",
        "--memory", "128m", "--memory-swap", "128m",
        "--cpus", "0.5",
        "--ulimit", "nofile=64:64",
        PINNED_REF,
    ] + list(PYTHON_ARGV)


def build_plan(code: str, *, name: str, owner: str = DEFAULT_OWNER) -> ExecutionPlan:
    """Fix the argv, and carry the bounded UTF-8 code over stdin instead."""
    return ExecutionPlan(argv=tuple(build_argv(name=name, owner=owner)),
                         stdin_code=code.encode("utf-8"),
                         name=name, owner=owner)
