"""Rendered by l9-semantic-compiler (pytest/v2). Driver fixture and outcome record."""

from __future__ import annotations

import importlib
import json
import os

import pytest

_OUTCOMES: dict[str, str] = {}


class SetupError(Exception):
    """A given step precondition that does not hold: an error of the case, never a failure."""


class L9Unresolved(Exception):
    """An unresolved case: reported, never passed and never skipped."""


class L9EvaluationError(Exception):
    """An assertion that evaluated to error (for example an absent relational path)."""


@pytest.fixture
def l9_driver():
    spec = os.environ.get("L9_CONFORMANCE_DRIVER", "")
    module, _, factory = spec.partition(":")
    if not module or not factory:
        raise SetupError("no conformance driver: set L9_CONFORMANCE_DRIVER=module:factory")
    driver = getattr(importlib.import_module(module), factory)()
    yield driver
    close = getattr(driver, "close", None)
    if close is not None:
        close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    case = getattr(item.function, "l9_case", None)
    if case is None:
        return
    if report.when == "setup" and report.failed:
        _OUTCOMES[case] = "error"
    elif report.when == "call":
        if report.passed:
            _OUTCOMES[case] = "pass"
        elif call.excinfo is not None and call.excinfo.errisinstance(L9Unresolved):
            _OUTCOMES[case] = "unresolved"
        elif call.excinfo is not None and call.excinfo.errisinstance(AssertionError):
            _OUTCOMES[case] = "fail"
        else:
            _OUTCOMES[case] = "error"
    elif report.when == "teardown" and report.failed:
        # A failing teardown (driver.close()) makes the case an error, whatever the call reported.
        _OUTCOMES[case] = "error"


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("L9_CONFORMANCE_OUTCOMES")
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_OUTCOMES, handle, sort_keys=True, indent=1)
