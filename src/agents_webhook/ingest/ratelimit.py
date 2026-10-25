"""Failed-authentication limit per client (design D18), in memory per ingest replica."""

import ipaddress
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable

WINDOW_SECONDS = 60.0

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class FailureLimiter:
    """A sliding 60 s window of failed authentications per client key.

    A key with `per_minute` failures inside the window is blocked until the oldest one leaves
    it. At most `max_keys` keys are tracked; the least recently failed are evicted first.
    """

    def __init__(
        self,
        per_minute: int,
        max_keys: int = 10000,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute: must be at least 1")
        if max_keys < 1:
            raise ValueError("max_keys: must be at least 1")
        self._per_minute = per_minute
        self._max_keys = max_keys
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._failures)

    def _live(self, key: str, now: float) -> deque[float] | None:
        times = self._failures.get(key)
        if times is None:
            return None
        while times and times[0] <= now - WINDOW_SECONDS:
            times.popleft()
        if not times:
            del self._failures[key]
            return None
        return times

    def blocked(self, key: str) -> float | None:
        """Seconds until `key` may try again, or None when it is not blocked."""
        now = self._clock()
        times = self._live(key, now)
        if times is None or len(times) < self._per_minute:
            return None
        return times[-self._per_minute] + WINDOW_SECONDS - now

    def record_failure(self, key: str) -> None:
        now = self._clock()
        times = self._live(key, now)
        if times is None:
            times = self._failures[key] = deque(maxlen=self._per_minute)
        times.append(now)
        self._failures.move_to_end(key)
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _trusted(value: str, networks: list[Network]) -> bool:
    address = _address(value)
    return address is not None and any(address in net for net in networks)


def parse_networks(cidrs: Iterable[str | Network]) -> list[Network]:
    return [ipaddress.ip_network(c, strict=False) if isinstance(c, str) else c for c in cidrs]


def client_key(
    peer_ip: str, x_forwarded_for: str | None, trusted_proxies: Iterable[str | Network]
) -> str:
    """The client a request is limited as.

    The peer address, unless the peer is a trusted proxy: then the right-most
    `X-Forwarded-For` hop that is not a trusted proxy (hops left of it are client-supplied and
    could be forged). When every hop is trusted, the left-most one.
    """
    networks = parse_networks(trusted_proxies)
    if not x_forwarded_for or not _trusted(peer_ip, networks):
        return peer_ip
    hops = [hop.strip() for hop in x_forwarded_for.split(",") if hop.strip()]
    if not hops:
        return peer_ip
    for hop in reversed(hops):
        if not _trusted(hop, networks):
            return hop
    return hops[0]
