"""Shared fixtures for the shipping test suite.

This file ships with the tests (it lives under tests/, which the sync script
copies wholesale minus tests/release and tests/private), so it must resolve
correctly in BOTH layouts:

  private (canonical HA config repo):
    watering-scheduler/tests/conftest.py
    release/geodrops-scheduler/static/examples/config.example.yaml

  public (built geodrops-scheduler repo, see release/geodrops-scheduler/sync.py):
    tests/conftest.py
    examples/config.example.yaml
"""
import os

import pytest


def _find_example_config():
    """Locate config.example.yaml by walking up from this file's own
    directory, checking both possible layouts at each level.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    checked = []
    current = here
    while True:
        public_candidate = os.path.join(current, "examples", "config.example.yaml")
        private_candidate = os.path.join(
            current, "release", "geodrops-scheduler", "static", "examples",
            "config.example.yaml",
        )
        for candidate in (public_candidate, private_candidate):
            checked.append(candidate)
            if os.path.isfile(candidate):
                return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise FileNotFoundError(
        "config.example.yaml not found in either the public (examples/) or "
        "private (release/geodrops-scheduler/static/examples/) layout, "
        "walking up from {}. Checked:\n{}".format(
            here, "\n".join(checked))
    )


@pytest.fixture
def example_config_path():
    """Path to config.example.yaml, resolved for whichever layout (public
    or private) the tests are currently running in.
    """
    return _find_example_config()
