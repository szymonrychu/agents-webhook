import base64
import re
import secrets

import psycopg
import pytest

from agents_webhook import credentials, filters
from agents_webhook.db import endpoints
from agents_webhook.db.endpoints import Credentials, EndpointIn, SuffixTaken

from .authhelp import authenticate

TYPES = ["grafana-alert", "gitlab-issue", "gitlab-mr", "github-issue", "github-pr"]
MIN = 16
URLSAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def fake_password() -> str:
    return secrets.token_urlsafe(24)


def basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


@pytest.fixture
async def conn(db):
    async with await psycopg.AsyncConnection.connect(db, autocommit=True) as c:
        yield c


async def _stored_hash(conn, endpoint_id: int) -> str:
    cur = await conn.execute("SELECT password_hash FROM endpoints WHERE id = %s", (endpoint_id,))
    return (await cur.fetchone())[0]


@pytest.fixture
def verify_spy(monkeypatch):
    calls: list[tuple[str, str]] = []
    real = credentials.verify_password

    def spy(password: str, stored: str) -> bool:
        calls.append((password, stored))
        return real(password, stored)

    monkeypatch.setattr(credentials, "verify_password", spy)
    return calls


# --- create ---------------------------------------------------------------------------------


async def test_create_defaults_generated(conn):
    ep, creds = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p1"), min_password_length=MIN
    )
    assert len(ep.suffix) == 32 and URLSAFE.match(ep.suffix)
    assert re.fullmatch(r"wh-[0-9a-f]{8}", creds.username)
    assert len(creds.password) == 43 and URLSAFE.match(creds.password)
    assert ep.username == creds.username
    assert ep.type == "github-pr" and ep.preset_id == "p1" and ep.prompt_template is None
    assert ep.created_at is not None and ep.credentials_rotated_at is None
    assert not hasattr(ep, "password_hash")
    other, other_creds = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p1"), min_password_length=MIN
    )
    assert other.suffix != ep.suffix and other_creds.password != creds.password


async def test_create_each_type(conn):
    for t in TYPES:
        ep, _ = await endpoints.create(
            conn, EndpointIn(type=t, preset_id="p"), min_password_length=MIN
        )
        assert ep.type == t


async def test_create_user_suffix_and_credentials_stored(conn):
    data = EndpointIn(
        type="grafana-alert",
        preset_id="alerts",
        suffix="my.hook_~-1",
        username="grafana",
        password="x" * MIN,
        prompt_template="Alert: {{ title }}",
    )
    ep, creds = await endpoints.create(conn, data, min_password_length=MIN)
    assert ep.suffix == "my.hook_~-1"
    assert creds == Credentials(username="grafana", password="x" * MIN)
    assert ep.prompt_template == "Alert: {{ title }}"
    assert await endpoints.get(conn, ep.id) == ep
    assert await endpoints.get_by_suffix(conn, "my.hook_~-1") == ep
    assert await authenticate(conn, ep.suffix, basic("grafana", "x" * MIN)) == ep


async def test_create_taken_suffix(conn):
    data = EndpointIn(type="github-pr", preset_id="p", suffix="taken")
    await endpoints.create(conn, data, min_password_length=MIN)
    with pytest.raises(SuffixTaken):
        await endpoints.create(conn, data, min_password_length=MIN)
    assert len(await endpoints.list_all(conn)) == 1


async def test_create_taken_suffix_inside_transaction_keeps_connection_usable(db):
    async with await psycopg.AsyncConnection.connect(db) as conn:
        data = EndpointIn(type="github-pr", preset_id="p", suffix="taken")
        async with conn.transaction():
            await endpoints.create(conn, data, min_password_length=MIN)
            with pytest.raises(SuffixTaken):
                await endpoints.create(conn, data, min_password_length=MIN)
            assert len(await endpoints.list_all(conn)) == 1


async def test_list_all_and_get_missing(conn):
    a, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN
    )
    b, _ = await endpoints.create(
        conn, EndpointIn(type="gitlab-mr", preset_id="q"), min_password_length=MIN
    )
    assert await endpoints.list_all(conn) == [a, b]
    assert await endpoints.get(conn, 999999) is None
    assert await endpoints.get_by_suffix(conn, "nope") is None


# --- invalid input --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix", ["", "has space", "a/b", "x" * 129, "ü", "a?b", "a#b", "a%2Fb", "a\x00b"]
)
async def test_create_invalid_suffix_named(conn, suffix):
    with pytest.raises(ValueError, match="suffix"):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", suffix=suffix),
            min_password_length=MIN,
        )
    assert await endpoints.list_all(conn) == []


async def test_create_valid_suffix_edges(conn):
    for suffix in ("x" * 128, "a", "A.b_c~d-e"):
        ep, _ = await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", suffix=suffix),
            min_password_length=MIN,
        )
        assert ep.suffix == suffix


@pytest.mark.parametrize("password", ["", "x" * (MIN - 1)])
async def test_create_invalid_short_password_named(conn, password):
    with pytest.raises(ValueError, match="password"):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", password=password),
            min_password_length=MIN,
        )
    assert await endpoints.list_all(conn) == []


async def test_create_password_length_follows_setting(conn):
    data = EndpointIn(type="github-pr", preset_id="p", password="x" * 20)
    with pytest.raises(ValueError, match="password"):
        await endpoints.create(conn, data, min_password_length=21)
    await endpoints.create(conn, data, min_password_length=20)


@pytest.mark.parametrize("username", ["", "a:b", "x" * 65, "tab\there", "new\nline", "nul\x00"])
async def test_create_invalid_username_named(conn, username):
    with pytest.raises(ValueError, match="username"):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", username=username),
            min_password_length=MIN,
        )
    assert await endpoints.list_all(conn) == []


async def test_create_valid_username_edges(conn):
    for i, username in enumerate(("u", "x" * 64, "with space", "żółw")):
        _, creds = await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", suffix=f"s{i}", username=username),
            min_password_length=MIN,
        )
        assert creds.username == username


async def test_create_invalid_type_and_preset_named(conn):
    with pytest.raises(ValueError, match="type"):
        await endpoints.create(
            conn, EndpointIn(type="bitbucket-pr", preset_id="p"), min_password_length=MIN
        )
    with pytest.raises(ValueError, match="preset_id"):
        await endpoints.create(
            conn, EndpointIn(type="github-pr", preset_id=""), min_password_length=MIN
        )


async def test_rotate_invalid_named(conn):
    ep, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN
    )
    with pytest.raises(ValueError, match="password"):
        await endpoints.rotate(conn, ep.id, password="short", min_password_length=MIN)
    with pytest.raises(ValueError, match="username"):
        await endpoints.rotate(conn, ep.id, username="a:b", min_password_length=MIN)


# --- hashing --------------------------------------------------------------------------------


def test_hash_format_and_roundtrip():
    stored = credentials.hash_password("correct horse battery")
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert len(base64.b64decode(salt)) == 16
    assert len(base64.b64decode(digest)) > 0
    assert "correct horse battery" not in stored
    assert credentials.verify_password("correct horse battery", stored)
    assert not credentials.verify_password("correct horse batterz", stored)
    assert not credentials.verify_password("", stored)


def test_hash_salt_is_random_per_call():
    a = credentials.hash_password("same password")
    b = credentials.hash_password("same password")
    assert a != b
    assert a.split("$")[4] != b.split("$")[4]


@pytest.mark.parametrize(
    "stored", ["", "plain", "scrypt$16384$8$1$!!$!!", "bcrypt$1$2$3$a$b", "scrypt$x$8$1$AA==$AA=="]
)
def test_hash_verify_malformed_stored_is_false(stored):
    assert credentials.verify_password("anything", stored) is False


def test_hash_verify_uses_compare_digest(monkeypatch):
    calls = []
    real = credentials.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(credentials.hmac, "compare_digest", spy)
    pw = fake_password()
    stored = credentials.hash_password(pw)
    assert credentials.verify_password(pw, stored)
    assert len(calls) == 1


def test_hash_dummy_is_scrypt_with_same_cost():
    assert credentials.DUMMY_HASH.split("$")[:4] == ["scrypt", "16384", "8", "1"]
    assert credentials.DUMMY_HASH is credentials.DUMMY_HASH  # computed once at import


async def test_hash_stored_per_endpoint_salt_never_password(conn):
    data = EndpointIn(type="github-pr", preset_id="p", password="p" * 20)
    a, _ = await endpoints.create(conn, data, min_password_length=MIN)
    b, _ = await endpoints.create(conn, data, min_password_length=MIN)
    ha, hb = await _stored_hash(conn, a.id), await _stored_hash(conn, b.id)
    assert ha.startswith("scrypt$16384$8$1$") and hb.startswith("scrypt$16384$8$1$")
    assert ha.split("$")[4] != hb.split("$")[4]
    assert "p" * 20 not in ha
    assert credentials.verify_password("p" * 20, ha)
    cur = await conn.execute("SELECT * FROM endpoints WHERE id = %s", (a.id,))
    assert "p" * 20 not in repr(await cur.fetchone())


def test_hash_credentials_and_input_repr_hide_password():
    pw = fake_password()
    assert pw not in repr(Credentials("u", pw))
    data = EndpointIn(type="github-pr", preset_id="p", password=pw)
    assert pw not in repr(data)


# --- parse_basic ----------------------------------------------------------------------------


def test_authenticate_parse_basic():
    assert credentials.parse_basic(basic("user", "pa:ss")) == ("user", "pa:ss")
    assert credentials.parse_basic("basic " + basic("u", "p").split()[1]) == ("u", "p")
    assert credentials.parse_basic(basic("żółw", "p")) == ("żółw", "p")
    for bad in (
        None,
        "",
        "Basic",
        "Basic ",
        "Bearer abc",
        "Basic !!!notbase64",
        "Basic " + base64.b64encode(b"nocolon").decode(),
        "Basic " + base64.b64encode(b"\xff\xfe:x").decode(),
        basic("u", "p") + " extra",
    ):
        assert credentials.parse_basic(bad) is None, bad


# --- authenticate ---------------------------------------------------------------------------


@pytest.fixture
async def endpoint(conn):
    data = EndpointIn(type="github-pr", preset_id="p", suffix="hook", username="alice")
    ep, creds = await endpoints.create(conn, data, min_password_length=MIN)
    return ep, creds


async def test_authenticate_success_only_with_all_three(conn, endpoint, verify_spy):
    ep, creds = endpoint
    stored = await _stored_hash(conn, ep.id)
    assert await authenticate(conn, "hook", basic("alice", creds.password)) == ep
    assert verify_spy == [(creds.password, stored)]


async def test_authenticate_unknown_suffix_uses_dummy_hash(conn, endpoint, verify_spy):
    _, creds = endpoint
    assert await authenticate(conn, "other", basic("alice", creds.password)) is None
    assert verify_spy == [(creds.password, credentials.DUMMY_HASH)]


async def test_authenticate_invalid_suffix_chars_uses_dummy_hash(conn, endpoint, verify_spy):
    _, creds = endpoint
    assert await authenticate(conn, "ho\x00ok", basic("alice", creds.password)) is None
    assert verify_spy == [(creds.password, credentials.DUMMY_HASH)]


async def test_authenticate_wrong_username(conn, endpoint, verify_spy):
    _, creds = endpoint
    assert await authenticate(conn, "hook", basic("bob", creds.password)) is None
    assert len(verify_spy) == 1


async def test_authenticate_wrong_password(conn, endpoint, verify_spy):
    assert await authenticate(conn, "hook", basic("alice", "w" * 43)) is None
    assert len(verify_spy) == 1


@pytest.mark.parametrize("header", [None, "", "Bearer x", "Basic ???", "Basic " + "YWJj"])
async def test_authenticate_malformed_header_uses_dummy_hash(conn, endpoint, verify_spy, header):
    assert await authenticate(conn, "hook", header) is None
    assert len(verify_spy) == 1
    assert verify_spy[0][1] == credentials.DUMMY_HASH


async def test_authenticate_deleted_endpoint_uses_dummy_hash(conn, endpoint, verify_spy):
    ep, creds = endpoint
    assert await endpoints.delete(conn, ep.id)
    assert await authenticate(conn, "hook", basic("alice", creds.password)) is None
    assert verify_spy == [(creds.password, credentials.DUMMY_HASH)]


async def test_authenticate_username_compared_in_constant_time(conn, endpoint, monkeypatch):
    _, creds = endpoint
    calls = []
    real = credentials.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(credentials.hmac, "compare_digest", spy)
    assert await authenticate(conn, "hook", basic("bob", creds.password)) is None
    # username compare plus the scrypt digest compare
    assert len(calls) == 2


# --- rotate ---------------------------------------------------------------------------------


async def test_rotate_invalidates_old_password(conn, endpoint):
    ep, old = endpoint
    new = await endpoints.rotate(conn, ep.id, min_password_length=MIN)
    assert new is not None
    assert new.password != old.password and len(new.password) == 43
    assert re.fullmatch(r"wh-[0-9a-f]{8}", new.username)
    assert await authenticate(conn, "hook", basic(old.username, old.password)) is None
    assert await authenticate(conn, "hook", basic(new.username, old.password)) is None
    authed = await authenticate(conn, "hook", basic(new.username, new.password))
    assert authed is not None and authed.id == ep.id
    assert authed.username == new.username
    assert authed.credentials_rotated_at is not None


async def test_rotate_user_supplied_credentials(conn, endpoint):
    ep, _ = endpoint
    new = await endpoints.rotate(
        conn, ep.id, username="carol", password="c" * MIN, min_password_length=MIN
    )
    assert new == Credentials(username="carol", password="c" * MIN)
    assert await authenticate(conn, "hook", basic("carol", "c" * MIN)) is not None


async def test_rotate_missing_endpoint_returns_none(conn):
    assert await endpoints.rotate(conn, 424242, min_password_length=MIN) is None


# --- delete ---------------------------------------------------------------------------------


async def test_delete_keeps_events_with_suffix(conn, endpoint):
    ep, _ = endpoint
    cur = await conn.execute(
        "INSERT INTO events (priority, endpoint_id, endpoint_suffix, type, source, kind, "
        "payload, dedup_key, status) VALUES (1, %s, %s, 'github-pr', 'github', 'mr', '{}', "
        "'k', 'new') RETURNING id",
        (ep.id, ep.suffix),
    )
    event_id = (await cur.fetchone())[0]
    assert await endpoints.delete(conn, ep.id) is True
    assert await endpoints.get(conn, ep.id) is None
    assert await endpoints.delete(conn, ep.id) is False
    cur = await conn.execute(
        "SELECT endpoint_id, endpoint_suffix FROM events WHERE id = %s", (event_id,)
    )
    assert await cur.fetchone() == (None, "hook")


# --- filters --------------------------------------------------------------------------------

RULES = {"deny": [{"field": "author", "match": "equals", "values": ["bot"]}]}


async def _count(conn) -> int:
    cur = await conn.execute("SELECT count(*) FROM endpoints")
    return (await cur.fetchone())[0]


async def test_filters_default_empty(conn):
    cur = await conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'endpoints' AND column_name = 'filters'"
    )
    assert await cur.fetchone() is not None
    ep, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN
    )
    assert ep.filters == {"allow": [], "deny": []}
    assert (await endpoints.get(conn, ep.id)).filters == {"allow": [], "deny": []}


async def test_create_stores_filters(conn):
    expected = filters.validate("github-pr", RULES)
    assert expected["deny"]
    ep, creds = await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", filters=RULES),
        min_password_length=MIN,
    )
    assert ep.filters == expected
    assert (await endpoints.get(conn, ep.id)).filters == expected
    assert (await endpoints.get_by_suffix(conn, ep.suffix)).filters == expected
    assert (await endpoints.list_all(conn))[0].filters == expected
    authed = await authenticate(conn, ep.suffix, basic(creds.username, creds.password))
    assert authed is not None and authed.filters == expected


async def test_create_invalid_filters_inserts_nothing(conn):
    with pytest.raises(ValueError, match=r"^filters"):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", filters={"bogus": []}),
            min_password_length=MIN,
        )
    assert await _count(conn) == 0


async def test_set_filters_replaces(conn):
    ep, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN
    )
    updated = await endpoints.set_filters(conn, ep.id, RULES)
    assert updated is not None
    assert updated.filters == filters.validate("github-pr", RULES)
    assert updated.suffix == ep.suffix
    assert (await endpoints.get(conn, ep.id)) == updated
    cleared = await endpoints.set_filters(conn, ep.id, {})
    assert cleared is not None and cleared.filters == filters.EMPTY
    assert await endpoints.set_filters(conn, ep.id + 1000, RULES) is None


async def test_set_filters_validates_against_type(conn):
    ep, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p", filters=RULES), min_password_length=MIN
    )
    before = await endpoints.get(conn, ep.id)
    bad = {"allow": [{"field": "alertname", "match": "equals", "values": ["x"]}]}
    with pytest.raises(ValueError, match=r"^filters"):
        await endpoints.set_filters(conn, ep.id, bad)
    assert await endpoints.get(conn, ep.id) == before


async def test_authenticate_failures_unchanged_with_filters(conn, verify_spy):
    ep, creds = await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", filters=RULES),
        min_password_length=MIN,
    )
    assert await authenticate(conn, "unknown", basic(creds.username, creds.password)) is None
    assert await authenticate(conn, ep.suffix, basic(creds.username, "wrong" * 5)) is None
    assert await authenticate(conn, ep.suffix, basic("nobody", creds.password)) is None
    assert await authenticate(conn, ep.suffix, None) is None
    assert len(verify_spy) == 4


# --- auth columns (migration 0006) ------------------------------------------------------------

_COLS = (
    "(suffix, type, preset_id, username, password_hash, auth_mode, auth_header, "
    "hmac_algorithm, hmac_prefix, hmac_timestamp_header, secret_ct, secret_kid)"
)


async def _raw(conn, suffix, **kw):
    row = {
        "username": None,
        "password_hash": None,
        "auth_mode": "header",
        "auth_header": "X-Gitlab-Token",
        "hmac_algorithm": None,
        "hmac_prefix": None,
        "hmac_timestamp_header": None,
        "secret_ct": b"ct",
        "secret_kid": "kid",
    } | kw
    await conn.execute(
        f"INSERT INTO endpoints {_COLS} VALUES (%s, 'github-pr', 'p', %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s)",
        (
            suffix,
            row["username"],
            row["password_hash"],
            row["auth_mode"],
            row["auth_header"],
            row["hmac_algorithm"],
            row["hmac_prefix"],
            row["hmac_timestamp_header"],
            row["secret_ct"],
            row["secret_kid"],
        ),
    )


async def test_existing_basic_row_survives_and_authenticates(conn):
    ep, creds = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p1"), min_password_length=MIN
    )
    assert ep.auth_mode == "basic"
    assert ep.auth_header is None and ep.hmac_algorithm is None and not ep.has_secret
    assert ep.updated_at is not None
    got = await authenticate(conn, ep.suffix, basic(creds.username, creds.password))
    assert got is not None and got.id == ep.id


async def test_valid_rows_for_each_mode(conn):
    await _raw(conn, "h")
    await _raw(conn, "b", auth_mode="bearer", auth_header=None)
    await _raw(conn, "m", auth_mode="hmac", hmac_algorithm="sha256", hmac_prefix="sha256=")
    got = {e.suffix: e for e in await endpoints.list_all(conn)}
    assert got["h"].auth_mode == "header" and got["h"].has_secret and got["h"].username is None
    assert got["b"].auth_mode == "bearer" and got["b"].auth_header is None
    assert got["m"].hmac_prefix == "sha256=" and got["m"].hmac_algorithm == "sha256"


@pytest.mark.parametrize(
    "kw",
    [
        {"auth_mode": "digest"},
        {"secret_ct": None},  # header without a secret
        {"secret_kid": None},
        {"auth_header": None},  # header mode needs the header name
        {"password_hash": "h"},  # non-basic mode must not keep a hash
        {"username": "u"},
        {"hmac_prefix": "x"},  # hmac fields only in hmac mode
        {"auth_mode": "bearer"},  # bearer takes no header name
        {"auth_mode": "hmac"},  # hmac needs the algorithm
        {"auth_mode": "hmac", "hmac_algorithm": "md5"},
    ],
)
async def test_checks_reject_bad_combos(conn, kw):
    with pytest.raises(psycopg.errors.CheckViolation):
        await _raw(conn, "x", **kw)


async def test_basic_mode_rejects_secret_and_missing_hash(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        await _raw(
            conn,
            "x",
            auth_mode="basic",
            auth_header=None,
            username="u",
            password_hash="h",
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        await _raw(
            conn,
            "y",
            auth_mode="basic",
            auth_header=None,
            secret_ct=None,
            secret_kid=None,
            username="u",
        )


async def test_updated_at_defaults_now(conn):
    await _raw(conn, "h")
    cur = await conn.execute("SELECT updated_at <= now() FROM endpoints WHERE suffix='h'")
    assert (await cur.fetchone())[0] is True
