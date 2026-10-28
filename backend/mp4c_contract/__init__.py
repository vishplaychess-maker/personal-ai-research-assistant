"""MP-4C - the shared backend<->executor contract.

Deliberately DEPENDENCY-FREE (stdlib only) so BOTH sides can import it:
  * the application derives/signs a grant,
  * the executor verifies it,
without either importing the other's package. The application must never import
executor_service; the executor image ships no application code.

'grant' holds the ONE definition of the signed execution grant. There is no
second copy and no second approval system.
"""

__all__ = []
