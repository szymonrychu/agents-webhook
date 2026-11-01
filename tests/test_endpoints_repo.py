import base64
import re
import secrets

import psycopg
import pytest

from agents_webhook import credentials, filters
from agents_webhook.db import endpoints
from agents_webhook.db.endpoints import Credentials, EndpointIn, SuffixTaken
from agents_webhook.secrets_box import SecretBoxError

from .authhelp import BOX, authenticate

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
        conn, EndpointIn(type="github-pr", preset_id="p1"), min_password_length=MIN, box=BOX
    )
    assert len(ep.suffix) == 32 and URLSAFE.match(ep.suffix)
    assert re.fullmatch(r"wh-[0-9a-f]{8}", creds.username)
    assert len(creds.password) == 43 and URLSAFE.match(creds.password)
    assert ep.username == creds.username
    assert ep.type == "github-pr" and ep.preset_id == "p1" and ep.prompt_template is None
    assert ep.created_at is not None and ep.credentials_rotated_at is None
    assert not hasattr(ep, "password_hash")
    other, other_creds = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p1"), min_password_length=MIN, box=BOX
    )
    assert other.suffix != ep.suffix and other_creds.password != creds.password


async def test_create_each_type(conn):
    for t in TYPES:
        ep, _ = await endpoints.create(
            conn, EndpointIn(type=t, preset_id="p"), min_password_length=MIN, box=BOX
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
    ep, creds = await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
    assert ep.suffix == "my.hook_~-1"
    assert creds == Credentials(username="grafana", password="x" * MIN)
    assert ep.prompt_template == "Alert: {{ title }}"
    assert await endpoints.get(conn, ep.id) == ep
    assert await endpoints.get_by_suffix(conn, "my.hook_~-1") == ep
    assert await authenticate(conn, ep.suffix, basic("grafana", "x" * MIN)) == ep


async def test_create_taken_suffix(conn):
    data = EndpointIn(type="github-pr", preset_id="p", suffix="taken")
    await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
    with pytest.raises(SuffixTaken):
        await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
    assert len(await endpoints.list_all(conn)) == 1


async def test_create_taken_suffix_inside_transaction_keeps_connection_usable(db):
    async with await psycopg.AsyncConnection.connect(db) as conn:
        data = EndpointIn(type="github-pr", preset_id="p", suffix="taken")
        async with conn.transaction():
            await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
            with pytest.raises(SuffixTaken):
                await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
            assert len(await endpoints.list_all(conn)) == 1


async def test_list_all_and_get_missing(conn):
    a, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN, box=BOX
    )
    b, _ = await endpoints.create(
        conn, EndpointIn(type="gitlab-mr", preset_id="q"), min_password_length=MIN, box=BOX
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
            box=BOX,
        )
    assert await endpoints.list_all(conn) == []


async def test_create_valid_suffix_edges(conn):
    for suffix in ("x" * 128, "a", "A.b_c~d-e"):
        ep, _ = await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", suffix=suffix),
            min_password_length=MIN,
            box=BOX,
        )
        assert ep.suffix == suffix


@pytest.mark.parametrize("password", ["", "x" * (MIN - 1)])
async def test_create_invalid_short_password_named(conn, password):
    with pytest.raises(ValueError, match="password"):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", password=password),
            min_password_length=MIN,
            box=BOX,
        )
    assert await endpoints.list_all(conn) == []


async def test_create_password_length_follows_setting(conn):
    data = EndpointIn(type="github-pr", preset_id="p", password="x" * 20)
    with pytest.raises(ValueError, match="password"):
        await endpoints.create(conn, data, min_password_length=21, box=BOX)
    await endpoints.create(conn, data, min_password_length=20, box=BOX)


@pytest.mark.parametrize("username", ["", "a:b", "x" * 65, "tab\there", "new\nline", "nul\x00"])
async def test_create_invalid_username_named(conn, username):
    with pytest.raises(ValueError, match="username"):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", username=username),
            min_password_length=MIN,
            box=BOX,
        )
    assert await endpoints.list_all(conn) == []


async def test_create_valid_username_edges(conn):
    for i, username in enumerate(("u", "x" * 64, "with space", "żółw")):
        _, creds = await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", suffix=f"s{i}", username=username),
            min_password_length=MIN,
            box=BOX,
        )
        assert creds.username == username


async def test_create_invalid_type_and_preset_named(conn):
    with pytest.raises(ValueError, match="type"):
        await endpoints.create(
            conn, EndpointIn(type="bitbucket-pr", preset_id="p"), min_password_length=MIN, box=BOX
        )
    with pytest.raises(ValueError, match="preset_id"):
        await endpoints.create(
            conn, EndpointIn(type="github-pr", preset_id=""), min_password_length=MIN, box=BOX
        )


async def test_rotate_invalid_named(conn):
    ep, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN, box=BOX
    )
    with pytest.raises(ValueError, match="password"):
        await endpoints.rotate(conn, ep.id, password="short", min_password_length=MIN, box=BOX)
    with pytest.raises(ValueError, match="username"):
        await endpoints.rotate(conn, ep.id, username="a:b", min_password_length=MIN, box=BOX)


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
    a, _ = await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
    b, _ = await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
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
    ep, creds = await endpoints.create(conn, data, min_password_length=MIN, box=BOX)
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
    new = await endpoints.rotate(conn, ep.id, min_password_length=MIN, box=BOX)
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
        conn, ep.id, username="carol", password="c" * MIN, min_password_length=MIN, box=BOX
    )
    assert new == Credentials(username="carol", password="c" * MIN)
    assert await authenticate(conn, "hook", basic("carol", "c" * MIN)) is not None


async def test_rotate_missing_endpoint_returns_none(conn):
    assert await endpoints.rotate(conn, 424242, min_password_length=MIN, box=BOX) is None


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
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN, box=BOX
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
        box=BOX,
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
            box=BOX,
        )
    assert await _count(conn) == 0


async def test_set_filters_replaces(conn):
    ep, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p"), min_password_length=MIN, box=BOX
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
        conn,
        EndpointIn(type="github-pr", preset_id="p", filters=RULES),
        min_password_length=MIN,
        box=BOX,
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
        box=BOX,
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
        conn, EndpointIn(type="github-pr", preset_id="p1"), min_password_length=MIN, box=BOX
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


# --- create per auth mode ---------------------------------------------------------------------


async def _secret_row(conn, endpoint_id):
    cur = await conn.execute(
        "SELECT secret_ct, secret_kid, password_hash, username FROM endpoints WHERE id = %s",
        (endpoint_id,),
    )
    return await cur.fetchone()


@pytest.mark.parametrize(
    ("mode", "kw"),
    [
        ("header", {"auth_header": "X-Gitlab-Token"}),
        ("bearer", {}),
        (
            "hmac",
            {
                "auth_header": "X-Hub-Signature-256",
                "hmac_prefix": "sha256=",
                "hmac_timestamp_header": "X-Ts",
            },
        ),
    ],
)
async def test_create_secret_modes(conn, mode, kw):
    ep, creds = await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", auth_mode=mode, **kw),
        min_password_length=MIN,
        box=BOX,
    )
    assert ep.auth_mode == mode and ep.has_secret and ep.username is None
    assert creds.username is None and creds.password is None
    assert len(creds.secret) == 43 and URLSAFE.match(creds.secret)
    ct, kid, password_hash, username = await _secret_row(conn, ep.id)
    assert creds.secret.encode() not in bytes(ct)
    assert kid == BOX.kid and password_hash is None and username is None
    assert BOX.decrypt(bytes(ct), aad=str(ep.id).encode()) == creds.secret
    assert ep.auth_header == kw.get("auth_header")
    assert ep.hmac_algorithm == ("sha256" if mode == "hmac" else None)
    assert ep.hmac_prefix == kw.get("hmac_prefix")
    assert creds.secret not in repr(creds)


async def test_create_aad_binds_the_endpoint_id(conn):
    ep, creds = await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", auth_mode="bearer"),
        min_password_length=MIN,
        box=BOX,
    )
    ct, *_ = await _secret_row(conn, ep.id)
    with pytest.raises(SecretBoxError):
        BOX.decrypt(bytes(ct), aad=str(ep.id + 1).encode())


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"auth_mode": "digest"}, "auth_mode"),
        ({"auth_mode": "header"}, "auth_header"),
        ({"auth_mode": "header", "auth_header": "bad header"}, "auth_header"),
        ({"auth_mode": "header", "auth_header": "X-A", "hmac_prefix": "p"}, "hmac_prefix"),
        ({"auth_mode": "bearer", "auth_header": "X-A"}, "auth_header"),
        ({"auth_mode": "hmac"}, "auth_header"),
        ({"auth_mode": "hmac", "auth_header": "X-A", "hmac_algorithm": "md5"}, "hmac_algorithm"),
        ({"auth_mode": "hmac", "auth_header": "X-A", "hmac_timestamp_header": "a b"}, "hmac"),
        ({"auth_mode": "hmac", "auth_header": "X-A", "hmac_prefix": "x" * 40}, "hmac_prefix"),
        ({"auth_mode": "basic", "auth_header": "X-A"}, "auth_header"),
        ({"auth_mode": "bearer", "password": "p" * 20}, "username"),
        ({"auth_mode": "bearer", "username": "u"}, "username"),
    ],
)
async def test_create_invalid_auth_named(conn, kw, field):
    with pytest.raises(ValueError, match=field):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", **kw),
            min_password_length=MIN,
            box=BOX,
        )
    assert await endpoints.list_all(conn) == []


async def test_create_secret_mode_failure_leaves_no_row(conn):
    await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", suffix="dup", auth_mode="bearer"),
        min_password_length=MIN,
        box=BOX,
    )
    with pytest.raises(SuffixTaken):
        await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id="p", suffix="dup", auth_mode="bearer"),
            min_password_length=MIN,
            box=BOX,
        )
    assert len(await endpoints.list_all(conn)) == 1


async def test_secret_mode_authenticates_through_load_for_auth(conn):
    ep, creds = await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", suffix="b", auth_mode="bearer"),
        min_password_length=MIN,
        box=BOX,
    )
    got = await authenticate(conn, "b", f"Bearer {creds.secret}")
    assert got == ep
    assert await authenticate(conn, "b", "Bearer nope") is None


# --- rotate -----------------------------------------------------------------------------------


async def test_rotate_secret_mode_changes_ciphertext_and_secret(conn):
    ep, old = await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", suffix="r", auth_mode="bearer"),
        min_password_length=MIN,
        box=BOX,
    )
    before, *_ = await _secret_row(conn, ep.id)
    new = await endpoints.rotate(conn, ep.id, min_password_length=MIN, box=BOX)
    after, *_ = await _secret_row(conn, ep.id)
    assert new.secret != old.secret and bytes(before) != bytes(after)
    assert new.username is None and new.password is None
    assert await authenticate(conn, "r", f"Bearer {old.secret}") is None
    assert await authenticate(conn, "r", f"Bearer {new.secret}") is not None
    refreshed = await endpoints.get(conn, ep.id)
    assert refreshed.credentials_rotated_at is not None and refreshed.updated_at > ep.updated_at


async def test_rotate_secret_mode_refuses_caller_chosen_credentials(conn):
    ep, _ = await endpoints.create(
        conn,
        EndpointIn(type="github-pr", preset_id="p", auth_mode="bearer"),
        min_password_length=MIN,
        box=BOX,
    )
    with pytest.raises(ValueError, match="password"):
        await endpoints.rotate(conn, ep.id, password="p" * 20, min_password_length=MIN, box=BOX)


async def test_rotate_missing_endpoint_is_none(conn):
    assert await endpoints.rotate(conn, 9999, min_password_length=MIN, box=BOX) is None


# --- patch ------------------------------------------------------------------------------------


async def _make(conn, mode="basic", **kw):
    data = EndpointIn(
        type="github-pr", preset_id="p", suffix=kw.pop("suffix", None), auth_mode=mode, **kw
    )
    return await endpoints.create(conn, data, min_password_length=MIN, box=BOX)


async def _patch(conn, id, **fields):
    return await endpoints.patch(conn, id, fields, min_password_length=MIN, box=BOX)


async def test_patch_each_plain_field(conn):
    ep, creds = await _make(conn, username="alice")
    result = await _patch(
        conn,
        ep.id,
        preset_id="p2",
        prompt_template="T {{ title }}",
        suffix="renamed",
        filters={"deny": [{"field": "author", "values": ["bot"]}]},
        username="alice2",
    )
    got = result.endpoint
    assert result.credentials is None
    assert (got.preset_id, got.prompt_template, got.suffix) == ("p2", "T {{ title }}", "renamed")
    assert got.filters["deny"][0]["values"] == ["bot"]
    assert got.username == "alice2" and got.type == ep.type and got.id == ep.id
    assert got.updated_at > ep.updated_at
    # the password still works with the new username and was not rotated
    assert await authenticate(conn, "renamed", basic("alice2", creds.password)) is not None
    assert got.credentials_rotated_at is None


async def test_patch_clears_nullable_fields_and_header_fields(conn):
    ep, _ = await _make(
        conn,
        "hmac",
        auth_header="X-Sig",
        hmac_prefix="sha256=",
        hmac_timestamp_header="X-Ts",
        prompt_template="x",
    )
    before, *_ = await _secret_row(conn, ep.id)
    got = (
        await _patch(
            conn,
            ep.id,
            prompt_template=None,
            hmac_timestamp_header=None,
            hmac_prefix=None,
            auth_header="X-New",
        )
    ).endpoint
    after, *_ = await _secret_row(conn, ep.id)
    assert got.prompt_template is None and got.hmac_timestamp_header is None
    assert got.hmac_prefix is None and got.auth_header == "X-New"
    assert bytes(before) == bytes(after)  # the secret stays when the mode does not change


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"type": "github-issue"}, "type"),
        ({"secret": "x"}, "secret"),
        ({"password": "p" * 20}, "password"),
        ({"preset_id": ""}, "preset_id"),
        ({"suffix": "bad suffix"}, "suffix"),
        ({"username": "a:b"}, "username"),
        ({"auth_header": "X-A"}, "auth_header"),  # basic takes none
        ({"filters": {"deny": [{"field": "nope", "values": ["x"]}]}}, "filters"),
        ({"auth_mode": "header"}, "auth_header"),
    ],
)
async def test_patch_invalid_named_and_nothing_written(conn, fields, field):
    ep, _ = await _make(conn)
    with pytest.raises(ValueError, match=field):
        await _patch(conn, ep.id, **fields)
    assert await endpoints.get(conn, ep.id) == ep


async def test_patch_username_refused_for_secret_modes(conn):
    ep, _ = await _make(conn, "bearer")
    with pytest.raises(ValueError, match="username"):
        await _patch(conn, ep.id, username="u")


async def test_patch_suffix_taken_and_rolled_back(conn):
    await _make(conn, suffix="taken")
    ep, _ = await _make(conn, suffix="mine")
    with pytest.raises(SuffixTaken):
        await _patch(conn, ep.id, suffix="taken", preset_id="p9")
    assert await endpoints.get(conn, ep.id) == ep


async def test_patch_missing_endpoint_is_none(conn):
    assert await _patch(conn, 9999, preset_id="x") is None


@pytest.mark.parametrize(
    ("start", "target", "fields"),
    [
        ("basic", "header", {"auth_header": "X-Gitlab-Token"}),
        ("basic", "hmac", {"auth_header": "X-Sig"}),
        ("header", "bearer", {}),
        ("bearer", "hmac", {"auth_header": "X-Sig", "hmac_prefix": "sha256="}),
        ("hmac", "basic", {}),
    ],
)
async def test_patch_mode_change_forces_new_secret(conn, start, target, fields):
    kw = {"auth_header": "X-Old"} if start in ("header", "hmac") else {}
    ep, old = await _make(conn, start, **kw)
    before = await _secret_row(conn, ep.id)
    result = await _patch(conn, ep.id, auth_mode=target, **fields)
    after = await _secret_row(conn, ep.id)
    assert result.endpoint.auth_mode == target and result.credentials is not None
    assert result.endpoint.credentials_rotated_at is not None
    if target == "basic":
        assert result.credentials.password and result.credentials.username
        assert after[0] is None and after[1] is None and after[2] is not None
        assert credentials.verify_password(result.credentials.password, after[2])
    else:
        assert result.credentials.secret and result.credentials.password is None
        assert after[2] is None and after[3] is None
        assert BOX.decrypt(bytes(after[0]), aad=str(ep.id).encode()) == result.credentials.secret
        if start == "basic":
            assert before[0] is None
        else:
            assert bytes(before[0]) != bytes(after[0])
            assert result.credentials.secret != old.secret


async def test_patch_mode_change_does_not_inherit_old_header_fields(conn):
    ep, _ = await _make(conn, "hmac", auth_header="X-Sig", hmac_prefix="sha256=")
    result = await _patch(conn, ep.id, auth_mode="header", auth_header="X-Tok")
    assert result.endpoint.hmac_prefix is None and result.endpoint.hmac_algorithm is None


async def test_patch_mode_change_to_same_mode_is_not_a_change(conn):
    ep, _ = await _make(conn, "bearer")
    before, *_ = await _secret_row(conn, ep.id)
    result = await _patch(conn, ep.id, auth_mode="bearer")
    after, *_ = await _secret_row(conn, ep.id)
    assert result.credentials is None and bytes(before) == bytes(after)


async def test_patch_is_one_transaction(conn):
    ep, _ = await _make(conn, "bearer")
    with pytest.raises(ValueError):
        await _patch(conn, ep.id, preset_id="changed", auth_mode="hmac")  # hmac needs a header
    assert (await endpoints.get(conn, ep.id)).preset_id == "p"


# --- clone ------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["basic", "header", "bearer", "hmac"])
async def test_clone_copies_config_not_credentials(conn, mode):
    kw = {"auth_header": "X-Sig", "hmac_prefix": "sha256="} if mode == "hmac" else {}
    if mode == "header":
        kw = {"auth_header": "X-Gitlab-Token"}
    src, src_creds = await _make(
        conn,
        mode,
        prompt_template="tpl",
        filters={"deny": [{"field": "author", "values": ["b"]}]},
        **kw,
    )
    clone, creds = await endpoints.clone(conn, src.id, min_password_length=MIN, box=BOX)
    assert clone.id != src.id and clone.suffix != src.suffix and len(clone.suffix) == 32
    assert (clone.type, clone.preset_id, clone.prompt_template) == (src.type, "p", "tpl")
    assert clone.filters == src.filters
    assert (clone.auth_mode, clone.auth_header, clone.hmac_prefix) == (
        src.auth_mode,
        src.auth_header,
        src.hmac_prefix,
    )
    s_row, c_row = await _secret_row(conn, src.id), await _secret_row(conn, clone.id)
    if mode == "basic":
        assert c_row[2] != s_row[2] and c_row[3] != s_row[3]
        assert creds.password != src_creds.password and creds.username != src_creds.username
    else:
        assert bytes(c_row[0]) != bytes(s_row[0])
        assert creds.secret != src_creds.secret
        assert BOX.decrypt(bytes(c_row[0]), aad=str(clone.id).encode()) == creds.secret
        # the copied ciphertext would not even decrypt under the new id
        with pytest.raises(SecretBoxError):
            BOX.decrypt(bytes(s_row[0]), aad=str(clone.id).encode())
    assert (await endpoints.get(conn, src.id)) == src  # the source is untouched


async def test_clone_overrides_and_errors(conn):
    src, _ = await _make(conn, "bearer", suffix="orig")
    clone, _ = await endpoints.clone(
        conn, src.id, suffix="copy", preset_id="other", min_password_length=MIN, box=BOX
    )
    assert (clone.suffix, clone.preset_id) == ("copy", "other")
    with pytest.raises(SuffixTaken):
        await endpoints.clone(conn, src.id, suffix="orig", min_password_length=MIN, box=BOX)
    assert await endpoints.clone(conn, 9999, min_password_length=MIN, box=BOX) is None
    assert len(await endpoints.list_all(conn)) == 2
