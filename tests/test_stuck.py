from cua.discovery.stuck import StuckDetector


def test_repeated_screen():
    d = StuckDetector(max_steps=25, repeat_limit=3)
    assert d.record("page A", True) is None
    assert d.record("page A", True) is None
    assert "same screen" in d.record("page A", True)


def test_step_budget():
    d = StuckDetector(max_steps=2)
    d.record("a", True)
    assert d.record("b", True) == "step budget exhausted"


def test_consecutive_failures():
    d = StuckDetector(max_steps=25)
    d.record("a", False)
    d.record("b", False)
    assert d.record("c", False) == "3 consecutive actions failed"
