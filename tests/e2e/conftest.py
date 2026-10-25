"""E2E fixtures: the real `agents-webhook` console script (migrate, ingest, api, worker) as
subprocesses against a Postgres database and a FakeOrchestrator served by uvicorn."""

import base64
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from agents_common.fake_orchestrator import FakeOrchestrator
from psycopg import sql

TOKEN = "e2e-token"
WAIT_SECONDS = 30.0
API = "/webhooks/api/v1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for[T](probe: Callable[[], T | None], what: str, timeout: float = WAIT_SECONDS) -> T:
    """Poll `probe` until it returns something truthy; fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(0.1)


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def console_script() -> str:
    found = Path(sys.executable).parent / "agents-webhook"
    return str(found) if found.exists() else shutil.which("agents-webhook") or "agents-webhook"


class FakeServer:
    """A FakeOrchestrator on a free port, served by uvicorn in a thread of the test process."""

    def __init__(self) -> None:
        self.fake = FakeOrchestrator(TOKEN)
        self.port = free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.fake.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()
        with httpx.Client() as http:
            wait_for(lambda: self._up(http), "the fake orchestrator", 15)

    def _up(self, http: httpx.Client) -> bool:
        try:
            return http.get(f"{self.url}/api/v1/presets").status_code == 401
        except httpx.TransportError:
            return False

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


class Stack:
    """One database, one fake orchestrator and the service processes, driven through HTTP."""

    def __init__(self, admin_url: str, env: dict[str, str], worker: bool) -> None:
        self.admin_url = admin_url
        self.db_name = f"e2e_{uuid.uuid4().hex}"
        self.extra_env = env
        self.with_worker = worker
        self.orch = FakeServer()
        self.fake = self.orch.fake
        self.ingest_port, self.api_port = free_port(), free_port()
        self.procs: dict[str, subprocess.Popen] = {}
        self.http = httpx.Client(timeout=30)
        self.preset_id = ""

    # lifecycle ------------------------------------------------------------------------------

    def _env(self, port: int) -> dict[str, str]:
        base = self.admin_url.rsplit("/", 1)[0]
        return {
            **os.environ,
            "WEBHOOK_DATABASE_URL": f"{base}/{self.db_name}",
            "WEBHOOK_ORCHESTRATOR_URL": self.orch.url,
            "WEBHOOK_ORCHESTRATOR_TOKEN": TOKEN,
            "WEBHOOK_HTTP_HOST": "127.0.0.1",
            "WEBHOOK_HTTP_PORT": str(port),
            "WEBHOOK_PUBLIC_BASE_URL": f"http://127.0.0.1:{self.ingest_port}",
            "WEBHOOK_POLL_SECONDS": "0.2",
            "WEBHOOK_SHUTDOWN_GRACE_SECONDS": "5",
            "WEBHOOK_BOT_LOGINS": "botty",
            "WEBHOOK_MAX_IN_PROGRESS": "20",
            "WEBHOOK_MAX_IN_PROGRESS_PER_ENDPOINT": "20",
            "WEBHOOK_AUTH_FAILURES_PER_MINUTE": "1000",
            **self.extra_env,
        }

    def _spawn(self, command: str, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [console_script(), command],
            env=self._env(port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def start(self) -> None:
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.db_name)))
        self.orch.start()
        self.preset_id = str(self.fake.add_preset("dev").id)
        migrate = subprocess.run(
            [console_script(), "migrate"],
            env=self._env(0),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert migrate.returncode == 0, migrate.stderr
        self.procs["ingest"] = self._spawn("ingest", self.ingest_port)
        self.procs["api"] = self._spawn("api", self.api_port)
        if self.with_worker:
            self.procs["worker"] = self._spawn("worker", 0)
        for name, port in (("ingest", self.ingest_port), ("api", self.api_port)):
            wait_for(lambda name=name, port=port: self._ready(name, port), f"{name} readyz", 30)

    def _ready(self, name: str, port: int) -> bool:
        code = self.procs[name].poll()
        assert code is None, f"{name} exited {code}: {self.procs[name].stderr.read()}"
        try:
            return self.http.get(f"http://127.0.0.1:{port}/readyz").status_code == 200
        except httpx.TransportError:
            return False

    def stop(self) -> None:
        self.http.close()
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stderr:
                proc.stderr.close()
        self.orch.stop()
        with (
            contextlib.suppress(psycopg.Error),
            psycopg.connect(self.admin_url, autocommit=True) as conn,
        ):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(self.db_name)
                )
            )

    # HTTP helpers ---------------------------------------------------------------------------

    @property
    def ingest(self) -> str:
        return f"http://127.0.0.1:{self.ingest_port}"

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.api_port}{API}"

    def preset(self, **spec: Any) -> str:
        return str(self.fake.add_preset(f"p-{uuid.uuid4().hex[:6]}", spec=spec).id)

    def create_endpoint(self, type: str, **body: Any) -> dict[str, Any]:
        body = {"type": type, "preset_id": self.preset_id, **body}
        r = self.http.post(f"{self.api}/endpoints", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def send(
        self,
        endpoint: dict[str, Any],
        headers: dict[str, str],
        payload: Any,
        *,
        password: str | None = None,
    ) -> httpx.Response:
        auth = basic(endpoint["username"], password or endpoint["password"])
        return self.http.post(endpoint["url"], headers={**auth, **headers}, json=payload)

    def event(self, id: int) -> dict[str, Any]:
        r = self.http.get(f"{self.api}/events/{id}")
        assert r.status_code == 200, r.text
        return r.json()

    def events(self, **params: Any) -> list[dict[str, Any]]:
        r = self.http.get(f"{self.api}/events", params={"limit": 200, **params})
        assert r.status_code == 200, r.text
        return r.json()

    def wait_status(self, id: int, status: str) -> dict[str, Any]:
        def probe():
            row = self.event(id)
            # an in-progress row is running once its cluster and message are recorded
            running = status != "in-progress" or (row["message_id"] and row["cluster_slug"])
            return row if row["status"] == status and running else None

        try:
            return wait_for(probe, f"event {id} to be {status}")
        except pytest.fail.Exception:
            pytest.fail(f"event {id} is {self.event(id)['status']}, expected {status}")

    def cluster(self, slug: str) -> dict[str, Any]:
        return wait_for(
            lambda: next((c for c in self.fake.clusters.values() if c["slug"] == slug), None),
            f"cluster {slug}",
        )

    def messages(self, cluster_id: str) -> list[dict[str, Any]]:
        return [m for m in self.fake.messages.values() if m["cluster_id"] == cluster_id]


def _started(admin_url: str, worker: bool, env: dict[str, str]) -> Stack:
    stack = Stack(admin_url, {f"WEBHOOK_{k.upper()}": v for k, v in env.items()}, worker)
    try:
        stack.start()
    except BaseException:
        stack.stop()
        raise
    return stack


@pytest.fixture(scope="session")
def stack(pg_url: str) -> Iterator[Stack]:
    """The shared stack: ingest, api and worker with generous caps."""
    shared = _started(pg_url, True, {})
    yield shared
    shared.stop()


@pytest.fixture
def make_stack(pg_url: str) -> Iterator[Callable[..., Stack]]:
    """Factory of a private stack for one test (`worker=False` leaves the queue untouched);
    each is stopped when the test ends. Settings come as keyword arguments (`max_queue="1"`)."""
    stacks: list[Stack] = []

    def make(worker: bool = True, **env: str) -> Stack:
        stacks.append(_started(pg_url, worker, env))
        return stacks[-1]

    yield make
    for each in reversed(stacks):
        each.stop()
