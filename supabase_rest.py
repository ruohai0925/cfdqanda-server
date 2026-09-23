"""Minimal stdlib-only Supabase client for the ops scripts.

stats.py and backfill_archive.py run wherever it is convenient — the host, the
API container, the GCP box — and the host has no `supabase` package installed.
urllib is always there, and these scripts only need REST, Storage and the admin
users endpoint, so this is the whole dependency footprint.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def load_env(env_path=None):
    """Load KEY="value" pairs from .env next to this file (does not override)."""
    path = Path(env_path or (Path(__file__).resolve().parent / ".env"))
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Supa:
    def __init__(self, url=None, key=None, timeout=120):
        load_env()
        self.url = (url or os.environ["SUPABASE_URL"]).strip('"').rstrip("/")
        self.key = (key or os.environ["SUPABASE_SERVICE_KEY"]).strip('"')
        self.timeout = timeout
        self.h = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}

    def _call(self, path, data=None, method=None, headers=None, raw=False):
        req = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={**self.h, **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read()
        if raw:
            return body
        return json.loads(body.decode()) if body.strip() else None

    # --- PostgREST ---
    def select(self, table, query="select=*", page=1000):
        """Fetch every row of a table, paging through PostgREST."""
        out, offset = [], 0
        while True:
            batch = self._call(f"/rest/v1/{table}?{query}&limit={page}&offset={offset}")
            out += batch
            if len(batch) < page:
                return out
            offset += page

    def upsert(self, table, rows, on_conflict="id"):
        if not rows:
            return 0
        self._call(
            f"/rest/v1/{table}?on_conflict={on_conflict}",
            data=json.dumps(rows).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        return len(rows)

    def table_exists(self, table):
        try:
            self._call(f"/rest/v1/{table}?select=*&limit=1")
            return True
        except urllib.error.HTTPError:
            return False

    # --- Auth admin ---
    def auth_users(self):
        out, page = [], 1
        while True:
            d = self._call(f"/auth/v1/admin/users?per_page=200&page={page}")
            batch = d.get("users", [])
            out += batch
            if len(batch) < 200:
                return out
            page += 1

    # --- Storage ---
    def storage_list(self, bucket, prefix, limit=1000):
        out, offset = [], 0
        while True:
            body = json.dumps({"prefix": prefix, "limit": limit, "offset": offset,
                               "sortBy": {"column": "name", "order": "asc"}}).encode()
            batch = self._call(f"/storage/v1/object/list/{bucket}", data=body, method="POST",
                               headers={"Content-Type": "application/json"})
            out += batch
            if len(batch) < limit:
                return out
            offset += limit

    def storage_get(self, bucket, path):
        return self._call(f"/storage/v1/object/{bucket}/{urllib.parse.quote(path)}", raw=True)
