"""Explicit opt-in wiring; no test store or calculation fixtures in runtime."""

_application = None


def get_application():
    global _application
    if _application is not None:
        return _application
    from mock_journey.aws_runtime import get_runtime
    _application = get_runtime("api").target
    return _application
