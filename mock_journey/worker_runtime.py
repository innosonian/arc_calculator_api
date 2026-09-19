"""Runtime requires explicit execution definitions and operating configuration."""

def get_worker():
    from mock_journey.aws_runtime import get_runtime
    return get_runtime("worker").target


def get_relay():
    from mock_journey.aws_runtime import get_runtime
    return get_runtime("relay").target
