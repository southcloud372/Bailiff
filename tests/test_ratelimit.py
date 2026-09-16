from agent_platform.gateway.ratelimit import RateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_token_bucket_limits_burst() -> None:
    clock = _FakeClock()
    rl = RateLimiter(capacity=3, refill_per_second=1.0, clock=clock)
    assert rl.allow("u1")
    assert rl.allow("u1")
    assert rl.allow("u1")
    assert not rl.allow("u1")  # 第 4 次超限


def test_token_bucket_refills() -> None:
    clock = _FakeClock()
    rl = RateLimiter(capacity=3, refill_per_second=1.0, clock=clock)
    for _ in range(3):
        rl.allow("u1")
    assert not rl.allow("u1")
    clock.advance(2.0)  # 2 秒补充 2 个 token
    assert rl.allow("u1")


def test_per_key_isolation() -> None:
    rl = RateLimiter(capacity=1, refill_per_second=0.0)
    assert rl.allow("u1")
    assert rl.allow("u2")  # 不同 key 互不影响
    assert not rl.allow("u1")
