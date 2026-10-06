from aiqa.runners.container import classify


def test_zero_collected_is_not_passed():
    assert classify({"collected": 0, "items": []}, 0) == "error"


def test_all_skipped_is_not_passed():
    assert classify({"collected": 1, "items": [{"outcome": "skipped", "phase": "call"}]}, 0) == "error"


def test_fixture_failure_is_execution_error():
    assert classify({"collected": 1, "items": [{"outcome": "failed", "phase": "setup"}]}, 1) == "error"


def test_assertion_failure_is_failed():
    assert classify({"collected": 1, "items": [{"outcome": "failed", "phase": "call"}]}, 1) == "failed"


def test_nonzero_process_cannot_be_passed():
    assert classify({"collected": 1, "items": [{"outcome": "passed", "phase": "call"}]}, 137) == "error"
