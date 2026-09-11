"""Run this checkout's tests with all external HTTP blocked."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import requests
import httpx

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
os.environ.setdefault("FEISHU_APP_ID", "offline-test")
os.environ.setdefault("FEISHU_APP_SECRET", "offline-test")
with patch.object(requests.sessions.Session, "request", side_effect=RuntimeError("External HTTP blocked")), \
     patch.object(httpx.HTTPTransport, "handle_request", side_effect=RuntimeError("External HTTP blocked")), \
     patch.object(httpx.AsyncHTTPTransport, "handle_async_request", side_effect=RuntimeError("External HTTP blocked")):
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.discover(str(root), pattern="test_*.py"))
sys.exit(not result.wasSuccessful())
