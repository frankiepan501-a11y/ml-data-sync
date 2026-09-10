"""Run unit tests against this checkout without real HTTP calls."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
with (
    patch.object(httpx.HTTPTransport, "handle_request", side_effect=RuntimeError("External HTTP blocked in tests")),
    patch.object(httpx.AsyncHTTPTransport, "handle_async_request", side_effect=RuntimeError("External HTTP blocked in tests")),
):
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    result = unittest.TextTestRunner().run(suite)
sys.exit(not result.wasSuccessful())
