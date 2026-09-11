from __future__ import annotations

import base64
import datetime
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from crau.cache.base import CacheBackend
from crau.models import NetworkTransaction, RawRequest, RawResponse


def _tx_to_dict(tx: NetworkTransaction) -> dict:
    return {
        "duration_seconds": tx.duration_seconds,
        "request": {
            "url": tx.request.url,
            "method": tx.request.method,
            "http_version": tx.request.http_version,
            "raw_headers": [
                [k.decode("latin1"), v.decode("latin1")] for k, v in tx.request.raw_headers
            ],
            "raw_body": base64.b64encode(tx.request.raw_body).decode("ascii"),
            "timestamp": tx.request.timestamp.isoformat(),
        },
        "response": {
            "status_code": tx.response.status_code,
            "reason_phrase": tx.response.reason_phrase,
            "http_version": tx.response.http_version,
            "raw_headers": [
                [k.decode("latin1"), v.decode("latin1")] for k, v in tx.response.raw_headers
            ],
            "raw_body": base64.b64encode(tx.response.raw_body).decode("ascii"),
            "timestamp": tx.response.timestamp.isoformat(),
        },
    }


def _dict_to_tx(data: dict) -> NetworkTransaction:
    req_d = data["request"]
    resp_d = data["response"]
    req = RawRequest(
        url=req_d["url"],
        method=req_d["method"],
        http_version=req_d["http_version"],
        raw_headers=[(k.encode("latin1"), v.encode("latin1")) for k, v in req_d["raw_headers"]],
        raw_body=base64.b64decode(req_d["raw_body"]),
        timestamp=datetime.datetime.fromisoformat(req_d["timestamp"]),
    )
    resp = RawResponse(
        status_code=resp_d["status_code"],
        reason_phrase=resp_d["reason_phrase"],
        http_version=resp_d["http_version"],
        raw_headers=[(k.encode("latin1"), v.encode("latin1")) for k, v in resp_d["raw_headers"]],
        raw_body=base64.b64decode(resp_d["raw_body"]),
        timestamp=datetime.datetime.fromisoformat(resp_d["timestamp"]),
    )
    return NetworkTransaction(
        request=req,
        response=resp,
        duration_seconds=data.get("duration_seconds", 0.0),
    )


class SqliteCacheBackend(CacheBackend):
    """Default high-performance SQLite-backed HTTP cache respecting RFC 7234."""

    def __init__(self, db_path: str | Path = ".crau_cache.sqlite"):
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    async def __aenter__(self) -> "SqliteCacheBackend":
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                url TEXT PRIMARY KEY,
                created_at REAL,
                expires_at REAL,
                data TEXT
            )
            """
        )
        self._conn.commit()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def get(self, url: str) -> list[NetworkTransaction] | None:
        if not self._conn:
            return None
        now = time.time()
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT expires_at, data FROM cache_entries WHERE url = ?",
            (url,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        expires_at, raw_json = row
        if expires_at is not None and expires_at < now:
            cursor.execute("DELETE FROM cache_entries WHERE url = ?", (url,))
            self._conn.commit()
            return None

        data_list = json.loads(raw_json)
        return [_dict_to_tx(item) for item in data_list]

    async def store(self, url: str, transactions: list[NetworkTransaction]) -> None:
        if not self._conn or not transactions:
            return

        final_tx = transactions[-1]
        cache_control = ""
        for name, value in final_tx.response.raw_headers:
            if name.lower() == b"cache-control":
                cache_control = value.decode("latin1").lower()
                break

        if "no-store" in cache_control:
            return

        now = time.time()
        expires_at = now + 86400  # Default 24h

        if "max-age=" in cache_control:
            try:
                for part in cache_control.split(","):
                    part = part.strip()
                    if part.startswith("max-age="):
                        max_age = int(part.split("=")[1])
                        expires_at = now + max_age
                        break
            except Exception:
                pass

        serialized = json.dumps([_tx_to_dict(t) for t in transactions])
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO cache_entries (url, created_at, expires_at, data)
                VALUES (?, ?, ?, ?)
                """,
                (url, now, expires_at, serialized),
            )
