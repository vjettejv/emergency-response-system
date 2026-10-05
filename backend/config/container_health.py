"""Docker readiness probe; output only exit status, never response/infrastructure."""
import json
import os
import sys
from urllib.request import Request, urlopen

try:
    host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip()
    with urlopen(Request("http://127.0.0.1:8000/api/health/", headers={"Host": host}), timeout=8) as response:
        healthy = response.status == 200 and json.load(response).get("status") == "ok"
except Exception:
    healthy = False
sys.exit(0 if healthy else 1)
