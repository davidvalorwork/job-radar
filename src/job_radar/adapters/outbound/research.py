"""Agent Reach routes, bounded local artifacts and SQLite evidence cache."""

import hashlib
import ipaddress
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from job_radar.adapters.outbound.config import read_yaml
from job_radar.application.collection import ResearchRequest, ResearchResponse

LIMIT = 1_048_576
URL = re.compile(r"https?://[^\s)\]\\\"<>]+", re.I)
JOB_PATH_TOKENS = ("job", "career", "empleo", "vacan", "opening", "position", "role")


def validate(request: ResearchRequest) -> None:
    if (
        request.kind not in {"web", "search"}
        or not request.value.strip()
        or len(request.value) > 4096
    ):
        raise ValueError("Invalid research request")
    if any(ord(char) < 32 for char in request.value):
        raise ValueError("Control characters not allowed")
    if request.kind == "web":
        url = urlsplit(request.value)
        host = url.hostname or ""
        if url.scheme != "https" or url.username or url.password or url.port not in {None, 443}:
            raise ValueError("Public HTTPS URLs required")
        if not host or "." not in host or host.endswith((".local", ".localhost", ".internal")):
            raise ValueError("Public host required")
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            return
        if not parsed.is_global:
            raise ValueError("Private addresses are not research targets")


def job_requests(
    path: Path, *, limit: int, after: int = 0, unseen_only: bool = False
) -> list[ResearchRequest]:
    """Extract bounded public job-page reads from locally cached discovery evidence."""
    if not 1 <= limit <= 1000 or after < 0 or unseen_only and after:
        raise ValueError("Invalid expansion budget")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        cached = (
            {
                row[0]
                for row in connection.execute(
                    "SELECT value FROM evidence WHERE kind='web' AND status='ok'"
                )
            }
            if unseen_only
            else set()
        )
        rows = connection.execute(
            "SELECT content FROM evidence WHERE kind IN ('search','web') AND status='ok' "
            "ORDER BY at"
        )
        result: list[ResearchRequest] = []
        seen: set[str] = set()
        skipped = 0
        for (content,) in rows:
            for raw in URL.findall(content):
                parsed = urlsplit(raw.rstrip(".,;:!?"))
                primary_hn = (
                    parsed.hostname == "news.ycombinator.com"
                    and parsed.path == "/item"
                    and bool(re.fullmatch(r"id=\d+", parsed.query))
                )
                if not primary_hn and not any(
                    token in parsed.path.casefold() for token in JOB_PATH_TOKENS
                ):
                    continue
                normalized = parsed._replace(fragment="").geturl()
                if normalized in seen or normalized in cached:
                    continue
                request = ResearchRequest("web", normalized)
                try:
                    validate(request)
                except ValueError:
                    continue
                seen.add(normalized)
                if skipped < after:
                    skipped += 1
                    continue
                result.append(request)
                if len(result) == limit:
                    return result
        return result
    finally:
        connection.close()


def read_requests(path: Path) -> list[ResearchRequest]:
    rows = read_yaml(path)
    if isinstance(rows, dict):
        if (
            set(rows) != {"executed", "config_hash", "count", "queries"}
            or rows["executed"] is not False
            or not isinstance(rows["config_hash"], str)
            or not isinstance(rows["queries"], list)
            or type(rows["count"]) is not int
            or not 0 <= rows["count"] <= 1000
            or rows["count"] != len(rows["queries"])
        ):
            raise ValueError("Invalid query plan")
        queries = rows["queries"]
        for row in queries:
            if (
                not isinstance(row, dict)
                or set(row) != {"source_id", "profile_id", "query"}
                or not all(isinstance(value, str) for value in row.values())
            ):
                raise ValueError("Invalid planned query")
        rows = [{"kind": "search", "value": row["query"]} for row in queries]
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Expected at most 1000 requests")
    requests = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"kind", "value"}:
            raise ValueError("Expected kind and value")
        if not isinstance(row["kind"], str) or not isinstance(row["value"], str):
            raise ValueError("Expected strings")
        request = ResearchRequest(**row)
        validate(request)
        requests.append(request)
    return requests


class EvidenceCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS evidence(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL,
          at REAL NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_audit(
          id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, at REAL NOT NULL, status TEXT NOT NULL);
        """)

    @staticmethod
    def key(request: ResearchRequest) -> str:
        return hashlib.sha256(json.dumps([request.kind, request.value]).encode()).hexdigest()

    def fresh(self, request: ResearchRequest, ttl: int) -> bool:
        row = self.db.execute(
            "SELECT at,status FROM evidence WHERE id=?", (self.key(request),)
        ).fetchone()
        return bool(row and 0 <= time.time() - row[0] < (ttl if row[1] == "ok" else min(ttl, 300)))

    def put(self, request: ResearchRequest, response: ResearchResponse) -> None:
        if len(response.content.encode()) > LIMIT:
            raise ValueError("Evidence too large")
        key, now = self.key(request), time.time()
        digest = hashlib.sha256(response.content.encode()).hexdigest()
        with self.db:
            previous = self.db.execute("SELECT status FROM evidence WHERE id=?", (key,)).fetchone()
            # A transient provider failure must not erase the last usable page.
            if response.status != "ok" and previous and previous[0] == "ok":
                self.db.execute(
                    "INSERT INTO research_audit(request_id,at,status) VALUES (?,?,?)",
                    (key, now, response.status),
                )
                return
            self.db.execute(
                "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?)",
                (key, request.kind, request.value, now, response.status, response.content, digest),
            )
            self.db.execute(
                "INSERT INTO research_audit(request_id,at,status) VALUES (?,?,?)",
                (key, now, response.status),
            )

    def close(self) -> None:
        self.db.close()


class AgentReachReader:
    def __init__(self) -> None:
        self.blocked: set[str] = set()
        self.lock = threading.Lock()

    def _command(self, request: ResearchRequest) -> list[str]:
        if request.kind == "web":
            executable = shutil.which("curl.exe") or shutil.which("curl")
            if not executable:
                raise ValueError("curl unavailable")
            return [
                executable,
                "--silent",
                "--show-error",
                "--fail",
                "--max-time",
                "35",
                "--max-filesize",
                str(LIMIT),
                "https://r.jina.ai/" + request.value,
            ]
        executable = shutil.which("mcporter")
        if not executable:
            raise ValueError("mcporter unavailable")
        prefix = [executable]
        if Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            # Avoid passing query text through cmd.exe/PowerShell. Use the npm entrypoint.
            entry = Path(executable).parent / "node_modules/mcporter/dist/cli.js"
            node = shutil.which("node")
            if not node or not entry.is_file():
                raise ValueError("Direct mcporter Node entrypoint unavailable")
            prefix = [node, str(entry)]
        return prefix + ["call", "exa.web_search_exa", "query=" + request.value, "numResults=10"]

    def fetch(self, request: ResearchRequest) -> ResearchResponse:
        validate(request)
        with self.lock:
            if request.kind in self.blocked:
                return ResearchResponse("provider_circuit_open", "")
        # Spool output, do not hold arbitrarily large subprocess output in RAM or model context.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            process = subprocess.Popen(
                self._command(request),
                stdout=output,
                stderr=error,
                stdin=subprocess.DEVNULL,
                shell=False,
            )
            started = time.monotonic()
            while process.poll() is None:
                if time.monotonic() - started > 45 or output.tell() > LIMIT or error.tell() > LIMIT:
                    process.kill()
                    process.wait()
                    return ResearchResponse("reader_budget_exceeded", "")
                time.sleep(0.05)
            output.seek(0)
            data = output.read(LIMIT + 1)
            if len(data) > LIMIT:
                return ResearchResponse("reader_budget_exceeded", "")
            text = data.decode("utf-8", errors="replace")
            error.seek(0)
            diagnostic = error.read(4096).decode("utf-8", errors="replace")
        # Provider errors occur in diagnostics or the response preamble, not in
        # downloaded articles (which may discuss rate limits or have numeric IDs).
        preamble = re.split(r"(?m)^Title:", text, maxsplit=1)[0]
        if re.search(
            r"\b429\b|rate.limit|quota.exceeded|free[^\n]*limit",
            preamble + diagnostic,
            re.I,
        ):
            with self.lock:
                self.blocked.add(request.kind)
            return ResearchResponse("provider_limited", "")
        if (
            process.returncode
            or not text.strip()
            or re.search(
                r"Target URL returned error|^\s*\{\s*\"code\"\s*:\s*[45]\d\d"
                r"|SecurityCompromiseError|^Title: Just a moment"
                r"|^Warning:[^\n]*CAPTCHA",
                text,
                re.M | re.I,
            )
        ):
            return ResearchResponse("reader_failed", "")
        return ResearchResponse("ok", text)
