"""Which merchants this system may EXECUTE against, as opposed to read.

Reading a real merchant's prices and placing a real order with one are different
authorities, and until now the Console did not distinguish them: the payment
route did `MockAdapter(order.cart["merchant"])`, so approving payment on a real
JioMart cart built a mock adapter pointed at a merchant mockstore has never
heard of. The failure mode is the dangerous kind — not a crash, but a
plausible-looking run that mints a PaymentMandate and writes a merchant order id
into the audit log for an order no merchant ever received. A chain that records
something that did not happen is worse than one that records nothing.

So execution is a positive allowlist of the three mock merchants, and a real
merchant raises. This is plan step 1f, and it is what makes "shop real
merchants" safe to hand to a human: every read path works end to end, and the
one step that would need authority this repo does not hold stops with an
explanation instead of pretending.
"""
from __future__ import annotations

from grocery.adapters.base import StorefrontAdapter
from grocery.adapters.mock import MockAdapter

# Positive, like every other guard here: a merchant added next month is
# non-executable by default rather than executable by oversight.
EXECUTABLE_MERCHANTS = frozenset({"freshmart", "quickbasket", "dailygrocer"})


class ExecutionUnavailable(RuntimeError):
    """This merchant's prices can be read but no order can be placed with it.
    Carries the sentence shown to the human verbatim."""


def is_executable(merchant: str) -> bool:
    return merchant in EXECUTABLE_MERCHANTS


def adapter_for(merchant: str, *, for_execution: bool) -> StorefrontAdapter:
    """The adapter the Console should use for `merchant`.

    for_execution=True is the carting/quoting/ordering path and is refused for
    any real merchant. for_execution=False is reserved for read-only Console
    use; it is a separate argument rather than a separate function so that a
    caller has to state which one it means.
    """
    if for_execution and not is_executable(merchant):
        raise ExecutionUnavailable(
            f"{merchant} is a real merchant — this system can read its prices but "
            "cannot place an order with it. Carting and payment are Phase 3/4 work "
            "and need authority this repo deliberately does not hold. The cart stays "
            "approved and nothing has been charged; abort it, or shop a mock merchant "
            "to exercise the payment path."
        )
    return MockAdapter(merchant)
