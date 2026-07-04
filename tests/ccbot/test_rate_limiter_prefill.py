"""Unit tests for `_prefill_limiter` (restart-time rate limiter pre-fill).

Verifies the fix for the bug where pre-filling `_level` alone was silently
undone by aiolimiter's leak calculation: `_last_check` (the clock reference
`_leak()` diffs against) must be stamped to "now" in the same call, otherwise
the very first `has_capacity()`/`acquire()` computes a huge elapsed time and
drains the bucket straight back to empty.
"""

import asyncio
import logging

import pytest
from aiolimiter import AsyncLimiter

from ccbot.bot import _prefill_limiter


class TestPrefillLimiter:
    @pytest.mark.asyncio
    async def test_bucket_is_full_immediately_after_prefill(self):
        limiter = AsyncLimiter(30, 1)

        # Simulate a limiter that was constructed a while ago (or has simply
        # been idle): push its clock reference into the past *before*
        # pre-filling, the way it would be if post_init ran long after
        # process start. Without the fix, pre-fill only sets `_level`, so
        # the next `_leak()` sees a huge elapsed time and drains it to zero.
        limiter._last_check = limiter._loop.time() - 100

        _prefill_limiter(limiter)

        assert limiter.has_capacity() is False

    @pytest.mark.asyncio
    async def test_capacity_recovers_after_one_drain_period(self):
        # max_rate=5 over time_period=0.1s -> one token drains every 0.02s.
        limiter = AsyncLimiter(5, 0.1)
        limiter._last_check = limiter._loop.time() - 100

        _prefill_limiter(limiter)
        assert limiter.has_capacity() is False

        # Sleep slightly over the time needed to drain one token's worth.
        await asyncio.sleep(0.05)

        assert limiter.has_capacity() is True

    @pytest.mark.asyncio
    async def test_skips_and_warns_when_attributes_missing(self, caplog):
        class _StubLimiter:
            """Stand-in for a future aiolimiter version lacking these attrs."""

            max_rate = 30.0

        stub = _StubLimiter()

        with caplog.at_level(logging.WARNING, logger="ccbot.bot"):
            _prefill_limiter(stub)  # type: ignore[arg-type]

        assert not hasattr(stub, "_level")
        assert not hasattr(stub, "_last_check")
        assert any(
            "_level" in record.getMessage() or "_last_check" in record.getMessage()
            for record in caplog.records
        )
