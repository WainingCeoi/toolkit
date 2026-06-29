import importlib.util
import logging
import warnings
from pathlib import Path

import pytest

# The page scripts run their Streamlit UI at import time; in "bare mode"
# (no ScriptRunContext) widgets no-op, so we can import them to reach the pure
# helper functions. Silence the resulting Streamlit warnings.
logging.getLogger("streamlit").setLevel(logging.CRITICAL)

PAGES = Path(__file__).resolve().parents[1] / "src" / "pages"


def _load(filename, name):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        spec = importlib.util.spec_from_file_location(name, PAGES / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def remux():
    return _load("remux_processor.py", "remux_processor_under_test")


@pytest.fixture(scope="session")
def gatherer():
    return _load("file_gatherer.py", "file_gatherer_under_test")


@pytest.fixture(scope="session")
def markdown():
    return _load("doc_to_markdown.py", "doc_to_markdown_under_test")
