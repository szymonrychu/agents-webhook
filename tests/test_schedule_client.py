"""The agents-schedule client: bearer auth, 404 handling, the token never logged."""

import json
import logging
import uuid

import httpx
import pytest

from agents_webhook.schedule_client import ScheduleClient, ScheduleUnavailable

SID = uuid.uuid4()
# An obvious placeholder, never a real token.
FAKE_TOKEN = "CANARY-schedule-admin-token"
BASE = "/schedules/api/v1/schedules"


def make(handler) -> ScheduleClient:
    return ScheduleClient("http://sched", FAKE_TOKEN, transport=httpx.MockTransport(handler))


async def test_get_schedule_true_on_200_and_sends_bearer():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": str(SID)})

    async with make(handler) as client:
        assert await client.get_schedule(SID) is True
    assert seen[0].method == "GET" and seen[0].url.path == f"{BASE}/{SID}"
    assert seen[0].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


async def test_get_schedule_false_on_404():
    async with make(lambda r: httpx.Response(404, json={})) as client:
        assert await client.get_schedule(SID) is False


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_get_schedule_other_status_is_unavailable(status):
    async with make(lambda r: httpx.Response(status)) as client:
        with pytest.raises(ScheduleUnavailable):
            await client.get_schedule(SID)


async def test_get_schedule_transport_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    async with make(handler) as client:
        with pytest.raises(ScheduleUnavailable):
            await client.get_schedule(SID)


@pytest.mark.parametrize("status", [200, 202])
async def test_chain_posts_the_exact_body(status):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={})

    async with make(handler) as client:
        await client.chain(
            SID,
            source_ref="42",
            previous_name="webhook:abc",
            previous_session_slug="wh-42",
            previous_result="the text",
        )
    request = seen[0]
    assert request.method == "POST" and request.url.path == f"{BASE}/{SID}/chain"
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert json.loads(request.content) == {
        "source": "webhook",
        "source_ref": "42",
        "previous_name": "webhook:abc",
        "previous_status": "handled",
        "previous_session_slug": "wh-42",
        "previous_result": "the text",
    }


async def test_chain_404_counts_as_done():
    async with make(lambda r: httpx.Response(404, json={})) as client:
        await client.chain(
            SID, source_ref="1", previous_name="n", previous_session_slug=None, previous_result=None
        )


@pytest.mark.parametrize("status", [400, 401, 409, 422, 500, 503])
async def test_chain_other_errors_are_unavailable(status):
    async with make(lambda r: httpx.Response(status)) as client:
        with pytest.raises(ScheduleUnavailable):
            await client.chain(
                SID,
                source_ref="1",
                previous_name="n",
                previous_session_slug=None,
                previous_result=None,
            )


async def test_token_never_in_logs_errors_or_repr(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(500, text=f"echo {FAKE_TOKEN}")

    async with make(handler) as client:
        with pytest.raises(ScheduleUnavailable) as info:
            await client.get_schedule(SID)
        assert FAKE_TOKEN not in str(info.value) and FAKE_TOKEN not in repr(client)
    assert FAKE_TOKEN not in caplog.text


def test_from_file_reads_the_token_once(tmp_path):
    path = tmp_path / "token"
    path.write_text(FAKE_TOKEN + "\n")
    client = ScheduleClient.from_file("http://sched/", str(path))
    assert client.base_url == "http://sched"
    path.write_text("changed")
    assert client._headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_from_file_empty_token_is_refused(tmp_path):
    path = tmp_path / "token"
    path.write_text("\n")
    with pytest.raises(ValueError):
        ScheduleClient.from_file("http://sched", str(path))
