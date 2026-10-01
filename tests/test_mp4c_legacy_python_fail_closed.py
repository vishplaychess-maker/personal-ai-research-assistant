"""MP-4C blocker regression: no model marker may reach code execution without a
PendingApproval and gateway dispatch.

Proven facts this pins (traced in the repo):
  * all FOUR legacy auto-exec blocks consult sandbox_policy.legacy_python_fail_closed
    (streaming_service x2, langgraph_workflow x2);
  * the in-process executors are imported but have NO call sites left in
    backend/app/services, so nothing there can start a subprocess;
  * sandbox_policy.legacy_may_execute_python() is hard False under EVERY flag;
  * real execution exists only behind the broker predicate
    (operator env opt-in + a live executor) - never the legacy path.
"""

from __future__ import annotations

import inspect

from app.config import settings
from app.run_engine import sandbox_policy

MARKER = "[PYTHON_CODE: print('canary')]"


def _code_only(source: str) -> str:
    return chr(10).join(ln for ln in source.splitlines()
                        if not ln.lstrip().startswith(("from ", "import ")))


def test_legacy_paths_may_never_execute_python():
    assert sandbox_policy.legacy_may_execute_python() is False


def test_marker_is_replaced_by_a_bounded_notice_never_code():
    out = sandbox_policy.legacy_python_fail_closed(MARKER)
    assert out == sandbox_policy.LEGACY_PYTHON_BLOCK_NOTICE
    assert "canary" not in out
    assert "print(" not in out
    assert sandbox_policy.legacy_python_fail_closed("plain prose") == ""


def test_the_flag_does_not_re_enable_legacy_execution(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    assert sandbox_policy.legacy_may_execute_python() is False
    assert sandbox_policy.legacy_python_fail_closed(MARKER) == \
        sandbox_policy.LEGACY_PYTHON_BLOCK_NOTICE


def test_every_legacy_block_consults_the_gate():
    import app.services.langgraph_workflow as lw
    import app.services.streaming_service as ss

    assert inspect.getsource(ss).count("legacy_python_fail_closed(") >= 2
    assert inspect.getsource(lw).count("legacy_python_fail_closed(") >= 2


def test_no_executor_call_site_remains_in_the_service_layer():
    import app.services.langgraph_workflow as lw
    import app.services.streaming_service as ss

    for mod in (ss, lw):
        body = _code_only(inspect.getsource(mod))
        for banned in ("run_python_code_async(", "run_python_code(",
                       "extract_python_code(", "create_subprocess"):
            assert banned not in body, (mod.__name__, banned)


def test_the_python_tool_is_not_bound_to_the_legacy_agent():
    import app.services.langgraph_workflow as lw

    lines = [ln for ln in inspect.getsource(lw).splitlines()
             if "python_sandbox" in ln]
    assert all(ln.lstrip().startswith("from ") or ln.lstrip().startswith("import ")
               for ln in lines), lines


def test_real_execution_is_unavailable_by_default(monkeypatch):
    monkeypatch.delenv(sandbox_policy.OPT_IN_ENV, raising=False)
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    assert sandbox_policy.operator_opt_in_enabled() is False
    assert sandbox_policy.real_python_execution_available() is False
