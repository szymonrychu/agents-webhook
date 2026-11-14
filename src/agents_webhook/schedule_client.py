"""Client of agents-schedule's admin listener: target checks and chained-run starts.

The bearer token is read once from a mounted file and sent only in the Authorization header;
it is never logged, never in an exception text and never in a repr.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Self

import httpx

log = logging.getLogger(__name__)

SCHEDULES_PATH = "/schedules/api/v1/schedules"
TIMEOUT_SECONDS = 10.0


class ScheduleUnavailable(Exception):
    """agents-schedule did not give a usable answer (down, refused, unexpected status)."""


class ScheduleClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=TIMEOUT_SECONDS, transport=transport
        )

    @classmethod
    def from_file(cls, base_url: str, token_file: str) -> Self:
        """Read the token once; ValueError when the file holds none, OSError when unreadable."""
        token = Path(token_file).read_text().strip()
        if not token:
            raise ValueError("schedule token file is empty")
        return cls(base_url, token)

    def __repr__(self) -> str:
        return f"ScheduleClient({self.base_url!r})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.request(method, path, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ScheduleUnavailable(
                f"agents-schedule unreachable ({type(exc).__name__})"
            ) from exc

    async def get_schedule(self, schedule_id: uuid.UUID) -> bool:
        """True when the schedule exists, False on 404; anything else is unavailable."""
        response = await self._request("GET", f"{SCHEDULES_PATH}/{schedule_id}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise ScheduleUnavailable(f"agents-schedule answered {response.status_code}")

    async def chain(
        self,
        schedule_id: uuid.UUID,
        *,
        source_ref: str,
        previous_name: str,
        previous_session_slug: str | None,
        previous_result: str | None,
    ) -> None:
        """Start one chained run of the schedule (idempotent per source_ref on its side).

        200 (already created) and 202 are done; 404 means the target was deleted, also done.
        """
        response = await self._request(
            "POST",
            f"{SCHEDULES_PATH}/{schedule_id}/chain",
            json={
                "source": "webhook",
                "source_ref": source_ref,
                "previous_name": previous_name,
                "previous_status": "handled",
                "previous_session_slug": previous_session_slug,
                "previous_result": previous_result,
            },
        )
        if response.status_code in (200, 202):
            return
        if response.status_code == 404:
            log.info("chain target %s is gone, counted as done", schedule_id)
            return
        raise ScheduleUnavailable(f"agents-schedule answered {response.status_code}")
