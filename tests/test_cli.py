import asyncio
import base64
import secrets
import uuid

import psycopg
import pytest
from psycopg import sql

from agents_webhook import cli
from agents_webhook.cli import main

SUBCOMMANDS = ["migrate", "ingest", "api", "worker"]
DB_URL = "postgresql://" + "u:" + "-".join(["fake", "pw"]) + "@localhost/db"
ENV = {
    "WEBHOOK_DATABASE_URL": DB_URL,
    "WEBHOOK_ORCHESTRATOR_URL": "http://orch:8001",
    "WEBHOOK_ORCHESTRATOR_TOKEN": "tok",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if key.startswith("WEBHOOK_"):
            monkeypatch.delenv(key)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    key_file = tmp_path / "endpoint-key"
    key_file.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.setenv("WEBHOOK_ENDPOINT_KEY_FILE", str(key_file))
    return monkeypatch


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_bad_settings_exit_78(name, env, capsys):
    env.delenv("WEBHOOK_DATABASE_URL")
    assert main([name]) == 78
    assert "WEBHOOK_DATABASE_URL" in capsys.readouterr().err


def test_bad_settings_names_every_variable(env, capsys):
    env.delenv("WEBHOOK_ORCHESTRATOR_TOKEN")
    env.setenv("WEBHOOK_TRUSTED_PROXIES", "nope")
    assert main(["api"]) == 78
    err = capsys.readouterr().err
    assert "WEBHOOK_ORCHESTRATOR_TOKEN" in err
    assert "WEBHOOK_TRUSTED_PROXIES" in err


def test_migrate_applies_schema(env, pg_url, caplog):
    name = f"cli_{uuid.uuid4().hex}"
    base = pg_url.rsplit("/", 1)[0]
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        env.setenv("WEBHOOK_DATABASE_URL", f"{base}/{name}")
        with caplog.at_level("INFO"):
            assert main(["migrate"]) == 0
        assert "migrations applied" in caplog.text
        with psycopg.connect(f"{base}/{name}") as conn:
            row = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        assert row[0] >= 1
        assert main(["migrate"]) == 0
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def test_config_ingest(env):
    settings = cli.load_settings(cli.WebhookSettings)
    config = cli.build_ingest_config(settings, pool=None, box=None)
    assert config.proxy_headers is False
    assert config.access_log is False
    assert config.timeout_keep_alive == 5
    assert config.http == "h11"
    assert config.h11_max_incomplete_event_size == cli.H11_MAX_INCOMPLETE_EVENT_SIZE
    assert config.host == settings.http_host
    assert config.port == settings.http_port
    assert config.timeout_graceful_shutdown == int(settings.shutdown_grace_seconds)
    assert isinstance(config.app, cli.BodyReadTimeout)
    assert config.app.app.title == "agents-webhook ingest"


def test_config_api(env):
    settings = cli.load_settings(cli.WebhookSettings)
    config = cli.build_api_config(settings, pool=None, client=None)
    assert config.proxy_headers is False
    assert config.host == settings.http_host
    assert config.port == settings.http_port
    assert config.app.title == "agents-webhook API"


def test_config_worker(env, monkeypatch):
    calls = {}

    class Pool:
        async def close(self):
            calls["closed"] = True

    async def fake_open_pool(url, *a, **k):
        calls["url"] = url
        return Pool()

    async def fake_run_worker(settings, pool, client, stop):
        calls["worker"] = (settings, pool, client, stop)

    monkeypatch.setattr(cli, "open_pool", fake_open_pool)
    monkeypatch.setattr(cli, "run_worker", fake_run_worker)
    assert main(["worker"]) == 0
    settings, pool, client, stop = calls["worker"]
    assert calls["url"] == DB_URL
    assert isinstance(pool, Pool)
    assert client._http.headers["Authorization"] == "Bearer tok"
    assert calls["closed"]


def test_config_serve_runs_server_with_pool(env, monkeypatch):
    calls = {}

    class Pool:
        async def close(self):
            calls["closed"] = True

    async def fake_open_pool(url, *a, **k):
        return Pool()

    class FakeServer:
        def __init__(self, config):
            calls["config"] = config
            self.should_exit = False

        async def serve(self):
            calls["served"] = True

    monkeypatch.setattr(cli, "open_pool", fake_open_pool)
    monkeypatch.setattr(cli, "_Server", FakeServer)
    assert main(["ingest"]) == 0
    assert calls["served"] and calls["closed"]
    assert calls["config"].proxy_headers is False


def _run_body_timeout(receive_script, seconds=0.05):
    sent = []

    async def app(scope, receive, send):
        msg = await receive()
        sent.append(("body", msg))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = cli.BodyReadTimeout(app, seconds)

    async def receive():
        return await receive_script()

    async def send(message):
        sent.append(message)

    asyncio.run(wrapped({"type": "http"}, receive, send))
    return sent


def test_config_body_read_timeout_answers_408():
    async def stall():
        await asyncio.sleep(5)

    sent = _run_body_timeout(stall)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 408


def test_config_body_read_timeout_passes_fast_bodies():
    async def fast():
        return {"type": "http.request", "body": b"x", "more_body": False}

    sent = _run_body_timeout(fast)
    assert sent[0][0] == "body"
    assert sent[1]["status"] == 200


def test_settings_per_subcommand(env, monkeypatch, capsys):
    env.delenv("WEBHOOK_ORCHESTRATOR_URL")
    env.delenv("WEBHOOK_ORCHESTRATOR_TOKEN")
    calls = {}

    class Pool:
        async def close(self):
            pass

    async def fake_open_pool(url, *a, **k):
        return Pool()

    class FakeServer:
        def __init__(self, config):
            calls["config"] = config
            self.should_exit = False

        async def serve(self):
            calls["served"] = True

    def no_client(*a, **k):
        raise AssertionError("ingest must not construct an orchestrator client")

    monkeypatch.setattr(cli, "open_pool", fake_open_pool)
    monkeypatch.setattr(cli, "_Server", FakeServer)
    monkeypatch.setattr(cli, "OrchestratorClient", no_client)
    monkeypatch.setattr(cli, "run_migrations", lambda url, name: ["0001"])
    assert main(["migrate"]) == 0
    assert main(["ingest"]) == 0
    assert calls["served"]
    for name in ("api", "worker"):
        assert main([name]) == 78
        err = capsys.readouterr().err
        assert "WEBHOOK_ORCHESTRATOR_URL" in err
        assert "WEBHOOK_ORCHESTRATOR_TOKEN" in err


def test_ingest_still_validates_own_settings(env, capsys):
    env.delenv("WEBHOOK_ORCHESTRATOR_URL")
    env.delenv("WEBHOOK_ORCHESTRATOR_TOKEN")
    env.setenv("WEBHOOK_TRUSTED_PROXIES", "nope")
    assert main(["ingest"]) == 78
    err = capsys.readouterr().err
    assert "WEBHOOK_TRUSTED_PROXIES" in err
    assert "ORCHESTRATOR" not in err


@pytest.mark.parametrize("content", [None, "not base64 !!", base64.b64encode(b"short").decode()])
def test_ingest_fails_fast_on_missing_or_bad_endpoint_key(env, capsys, tmp_path, content):
    if content is None:
        env.setenv("WEBHOOK_ENDPOINT_KEY_FILE", str(tmp_path / "absent"))
    else:
        bad = tmp_path / "bad"
        bad.write_text(content)
        env.setenv("WEBHOOK_ENDPOINT_KEY_FILE", str(bad))
    assert main(["ingest"]) == 78
    assert "WEBHOOK_ENDPOINT_KEY_FILE" in capsys.readouterr().err


def test_ingest_fails_fast_when_endpoint_key_unset(env, capsys):
    env.delenv("WEBHOOK_ENDPOINT_KEY_FILE")
    assert main(["ingest"]) == 78
    assert "WEBHOOK_ENDPOINT_KEY_FILE" in capsys.readouterr().err
