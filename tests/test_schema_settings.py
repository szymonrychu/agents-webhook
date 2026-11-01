import psycopg
import pytest
from agents_common.settings import SettingsError, load_settings

from agents_webhook.settings import WebhookSettings

TYPES = ["grafana-alert", "gitlab-issue", "gitlab-mr", "github-issue", "github-pr"]


def _endpoint(conn, suffix="a", type_="github-pr"):
    return conn.execute(
        "INSERT INTO endpoints (suffix, type, preset_id, username, password_hash) "
        "VALUES (%s, %s, 'p', 'u', 'h') RETURNING id",
        (suffix, type_),
    ).fetchone()[0]


def _event(conn, endpoint_id, key="k", status="new", source="github", kind="mr"):
    return conn.execute(
        "INSERT INTO events (priority, endpoint_id, endpoint_suffix, type, source, kind, "
        "payload, dedup_key, status) VALUES (1, %s, 'a', 'github-pr', %s, %s, '{}', %s, %s) "
        "RETURNING id",
        (endpoint_id, source, kind, key, status),
    ).fetchone()[0]


def test_migration_applies_and_tables_exist(db):
    with psycopg.connect(db) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
        }
    assert {"endpoints", "events"} <= names


def test_new_key_unique_per_endpoint_and_key(db):
    with psycopg.connect(db, autocommit=True) as conn:
        e1, e2 = _endpoint(conn, "a"), _endpoint(conn, "b")
        _event(conn, e1)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _event(conn, e1)
        _event(conn, e2)  # other endpoint
        _event(conn, e1, key="other")  # other key
        _event(conn, e1, status="in-progress")
        _event(conn, e1, status="handled")
        _event(conn, e1, status="handled")


def test_new_key_free_again_after_status_moves(db):
    with psycopg.connect(db, autocommit=True) as conn:
        e1 = _endpoint(conn)
        first = _event(conn, e1)
        conn.execute("UPDATE events SET status='in-progress' WHERE id=%s", (first,))
        _event(conn, e1)


def test_check_status_type_source_kind_suffix(db):
    with psycopg.connect(db, autocommit=True) as conn:
        e1 = _endpoint(conn)
        for bad in (
            lambda: _event(conn, e1, key="s", status="bogus"),
            lambda: _event(conn, e1, key="t", source="bitbucket"),
            lambda: _event(conn, e1, key="u", kind="other"),
            lambda: _endpoint(conn, "x", "bogus"),
            lambda: _endpoint(conn, "has space"),
            lambda: _endpoint(conn, "a/b"),
            lambda: _endpoint(conn, ""),
            lambda: _endpoint(conn, "x" * 129),
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                bad()
        for i, t in enumerate(TYPES):
            _endpoint(conn, f"ok.{i}_~-", t)
        _endpoint(conn, "x" * 128)
        for status in ("new", "in-progress", "handled", "ignored", "failed"):
            _event(conn, e1, key=status, status=status)


def test_check_no_plaintext_password_column(db):
    with psycopg.connect(db) as conn:
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='endpoints'"
            )
        }
    assert "password_hash" in cols
    assert "password" not in cols


def test_endpoint_delete_sets_event_endpoint_null(db):
    with psycopg.connect(db, autocommit=True) as conn:
        e1 = _endpoint(conn)
        ev = _event(conn, e1)
        conn.execute("DELETE FROM endpoints WHERE id=%s", (e1,))
        assert conn.execute("SELECT endpoint_id FROM events WHERE id=%s", (ev,)).fetchone() == (
            None,
        )


@pytest.fixture
def base_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("WEBHOOK_ORCHESTRATOR_URL", "http://orch")
    monkeypatch.setenv("WEBHOOK_ORCHESTRATOR_TOKEN", "t")


def test_settings_defaults(base_env):
    s = load_settings(WebhookSettings)
    assert s.public_base_url == "https://lab.szymonrichert.pl"
    assert s.max_in_progress == 3
    assert s.max_in_progress_per_endpoint == 1
    assert s.max_queue == 500
    assert s.max_body_bytes == 5 * 1024 * 1024
    assert s.run_timeout_seconds == 3600
    assert s.poll_seconds == 5
    assert s.events_retention_days == 30
    assert s.auth_failures_per_minute == 10
    assert s.min_password_length == 16
    assert s.bot_logins == [] and s.trusted_proxies == []


def test_allowed_authors_retired(base_env, monkeypatch):
    assert "allowed_authors" not in WebhookSettings.model_fields
    baseline = load_settings(WebhookSettings).model_dump()
    monkeypatch.setenv("WEBHOOK_ALLOWED_AUTHORS", "alice,bob")
    s = load_settings(WebhookSettings)
    assert not hasattr(s, "allowed_authors")
    assert s.model_dump() == baseline


def test_settings_env_overrides_and_trailing_slash(base_env, monkeypatch):
    monkeypatch.setenv("WEBHOOK_PUBLIC_BASE_URL", "https://x.example/")
    monkeypatch.setenv("WEBHOOK_MAX_IN_PROGRESS", "7")
    monkeypatch.setenv("WEBHOOK_MAX_IN_PROGRESS_PER_ENDPOINT", "2")
    monkeypatch.setenv("WEBHOOK_MAX_QUEUE", "9")
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", "100")
    monkeypatch.setenv("WEBHOOK_BOT_LOGINS", "a, b")
    monkeypatch.setenv("WEBHOOK_TRUSTED_PROXIES", "10.0.0.0/8, 2001:db8::/32")
    s = load_settings(WebhookSettings)
    assert s.public_base_url == "https://x.example"
    assert (s.max_in_progress, s.max_in_progress_per_endpoint) == (7, 2)
    assert (s.max_queue, s.max_body_bytes) == (9, 100)
    assert s.bot_logins == ["a", "b"]
    assert s.trusted_proxies == ["10.0.0.0/8", "2001:db8::/32"]


def test_settings_bad_cidr_named(base_env, monkeypatch):
    monkeypatch.setenv("WEBHOOK_TRUSTED_PROXIES", "10.0.0.0/8,nonsense")
    with pytest.raises(SettingsError) as exc:
        load_settings(WebhookSettings)
    assert "WEBHOOK_TRUSTED_PROXIES" in exc.value.problems
    assert "nonsense" in exc.value.problems["WEBHOOK_TRUSTED_PROXIES"]


def test_settings_every_bad_variable_named(monkeypatch):
    monkeypatch.setenv("WEBHOOK_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("WEBHOOK_ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("WEBHOOK_MAX_QUEUE", "lots")
    monkeypatch.setenv("WEBHOOK_MAX_IN_PROGRESS", "0")
    monkeypatch.delenv("WEBHOOK_ORCHESTRATOR_URL", raising=False)
    with pytest.raises(SettingsError) as exc:
        load_settings(WebhookSettings)
    assert {
        "WEBHOOK_MAX_QUEUE",
        "WEBHOOK_MAX_IN_PROGRESS",
        "WEBHOOK_ORCHESTRATOR_URL",
    } <= set(exc.value.problems)


def test_settings_endpoint_key_and_replay_defaults(base_env):
    s = load_settings(WebhookSettings)
    assert s.endpoint_key_file == ""
    assert s.hmac_replay_seconds == 300


def test_settings_endpoint_key_and_replay_env(base_env, monkeypatch):
    monkeypatch.setenv("WEBHOOK_ENDPOINT_KEY_FILE", "/run/key")
    monkeypatch.setenv("WEBHOOK_HMAC_REPLAY_SECONDS", "60")
    s = load_settings(WebhookSettings)
    assert (s.endpoint_key_file, s.hmac_replay_seconds) == ("/run/key", 60)


def test_settings_replay_must_be_positive(base_env, monkeypatch):
    monkeypatch.setenv("WEBHOOK_HMAC_REPLAY_SECONDS", "0")
    with pytest.raises(SettingsError) as exc:
        load_settings(WebhookSettings)
    assert "WEBHOOK_HMAC_REPLAY_SECONDS" in exc.value.problems
