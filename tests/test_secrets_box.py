import base64
import secrets

import pytest

from agents_webhook import credentials
from agents_webhook.secrets_box import KEY_BYTES, SecretBox, SecretBoxError


def _key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def _write(tmp_path, raw: bytes, name="key"):
    path = tmp_path / name
    path.write_text(base64.b64encode(raw).decode() + "\n")
    return str(path)


def test_round_trip():
    box = SecretBox(_key())
    plain = secrets.token_urlsafe(32)
    blob = box.encrypt(plain, aad=b"7")
    assert plain.encode() not in blob
    assert box.decrypt(blob, aad=b"7") == plain


def test_fresh_nonce_per_encryption():
    box = SecretBox(_key())
    assert box.encrypt("same", aad=b"1") != box.encrypt("same", aad=b"1")


def test_blob_layout_nonce_ciphertext_tag():
    box = SecretBox(_key())
    assert len(box.encrypt("abc", aad=b"1")) == 12 + 3 + 16


def test_tamper_fails():
    box = SecretBox(_key())
    blob = bytearray(box.encrypt("secret", aad=b"1"))
    blob[-1] ^= 1
    with pytest.raises(SecretBoxError):
        box.decrypt(bytes(blob), aad=b"1")


def test_wrong_aad_fails():
    box = SecretBox(_key())
    blob = box.encrypt("secret", aad=b"1")
    with pytest.raises(SecretBoxError):
        box.decrypt(blob, aad=b"2")


def test_wrong_key_fails():
    blob = SecretBox(_key()).encrypt("secret", aad=b"1")
    with pytest.raises(SecretBoxError):
        SecretBox(_key()).decrypt(blob, aad=b"1")


def test_short_blob_fails():
    with pytest.raises(SecretBoxError):
        SecretBox(_key()).decrypt(b"short", aad=b"1")


def test_kid_is_stable_fingerprint_not_the_key():
    raw = _key()
    box = SecretBox(raw)
    assert box.kid == SecretBox(raw).kid
    assert box.kid != SecretBox(_key()).kid
    assert len(box.kid) == 16
    assert base64.b64encode(raw).decode() not in box.kid


def test_constructor_rejects_bad_length():
    with pytest.raises(SecretBoxError):
        SecretBox(b"x" * 16)


def test_from_file(tmp_path):
    raw = _key()
    box = SecretBox.from_file(_write(tmp_path, raw))
    assert box.decrypt(SecretBox(raw).encrypt("v", aad=b"1"), aad=b"1") == "v"


def test_from_file_missing_fails(tmp_path):
    with pytest.raises(SecretBoxError, match="not readable"):
        SecretBox.from_file(str(tmp_path / "nope"))


def test_from_file_empty_path_fails():
    with pytest.raises(SecretBoxError, match="WEBHOOK_ENDPOINT_KEY_FILE"):
        SecretBox.from_file("")


@pytest.mark.parametrize("size", [16, 31, 33, 64])
def test_from_file_wrong_length_fails(tmp_path, size):
    with pytest.raises(SecretBoxError, match="32 bytes"):
        SecretBox.from_file(_write(tmp_path, secrets.token_bytes(size)))


def test_from_file_not_base64_fails(tmp_path):
    path = tmp_path / "k"
    path.write_text("!!! not base64 !!!")
    with pytest.raises(SecretBoxError, match="base64"):
        SecretBox.from_file(str(path))


def test_error_never_contains_key_material(tmp_path):
    raw = secrets.token_bytes(16)
    with pytest.raises(SecretBoxError) as exc:
        SecretBox.from_file(_write(tmp_path, raw))
    assert base64.b64encode(raw).decode() not in str(exc.value)


def test_generate_secret():
    a, b = credentials.generate_secret(), credentials.generate_secret()
    assert a != b
    assert len(a) == 43
