"""One poll pass over the account's units, and the per-unit grace
bookkeeping. No Home Assistant imports, so plain pytest can exercise it."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .api import (
    AquareaHomeError,
    AuthError,
    DeviceOffline,
    RequestTimeout,
    UnitForbidden,
)

_LOGGER = logging.getLogger(__name__)

# this many timeouts with no unit answering at all = the cloud, not the units
_TIMEOUTS_BEFORE_GIVING_UP = 2


def all_forbidden(fresh: dict[str, dict], failed: dict[str, Exception]) -> bool:
    """Every unit refused and none answered: that looks like the token rather
    than the units, until a fresh token is refused as well."""
    return bool(failed) and not fresh and all(
        isinstance(err, UnitForbidden) for err in failed.values())


async def poll_units(
    client: Any, devices: list[dict], *,
    answered_last: set[str] | None = None,
) -> tuple[dict[str, dict], dict[str, Exception]]:
    """Ask every unit for its state, in turn. Returns (fresh, failed) by MAC.

    A DeviceOffline is that unit's problem and the pass carries on. Any other
    error means the path to the cloud is broken: the remaining units would
    fail the same way (at up to GRPC_TIMEOUT each), so they are marked failed
    with the same error and the pass ends. AuthError propagates — the caller
    owns the re-login.

    Units that answered last time are asked first, so that timeouts with
    nothing answering really mean "nobody who answered before answers now"
    and not "the two slow units happen to be listed first". With no history
    (first pass) every unit is asked."""
    answered_last = answered_last or set()
    devices = sorted(devices, key=lambda d: d["mac"] not in answered_last)
    give_up_after = _TIMEOUTS_BEFORE_GIVING_UP if answered_last else len(devices)
    fresh: dict[str, dict] = {}
    failed: dict[str, Exception] = {}
    timeouts = 0
    cloud_answered = False
    for i, dev in enumerate(devices):
        mac = dev["mac"]
        try:
            fresh[mac] = await client.get_state(mac, dev.get("node_id", 0))
            cloud_answered = True
            continue
        except AuthError:
            raise
        except DeviceOffline as err:
            failed[mac] = err
            if not isinstance(err, RequestTimeout):
                cloud_answered = True   # "this unit is offline" is an answer
                continue
            timeouts += 1
            if cloud_answered or timeouts < give_up_after:
                continue
        except (AquareaHomeError, asyncio.TimeoutError) as err:
            failed[mac] = err
        for rest in devices[i + 1:]:
            failed[rest["mac"]] = failed[mac]
        break
    return fresh, failed


class UnitBook:
    """Per-unit miss counters. A unit that misses a poll keeps its last-known
    state for `grace - 1` polls; after that it is left out of the data, which
    is what makes its entities — and only its entities — unavailable."""

    def __init__(self, names: dict[str, str], grace: int) -> None:
        self._names = names
        self._grace = grace
        self._misses: dict[str, int] = {}
        self._down: set[str] = set()
        self._quiet: set[str] = set()   # dropped while nothing answered

    def settle(self, previous: dict[str, dict], fresh: dict[str, dict],
               failed: dict[str, Exception]) -> dict[str, dict]:
        """Merge one pass into the data the entities will see."""
        data = dict(fresh)
        for mac in fresh:
            self.reached(mac)
        dropped: list[str] = []
        for mac, err in failed.items():
            misses = self._misses[mac] = self._misses.get(mac, 0) + 1
            if misses < self._grace and mac in previous:
                data[mac] = previous[mac]
                _LOGGER.debug("%s missed a poll (%s); keeping its last state (%d/%d)",
                              self._name(mac), err, misses, self._grace)
            elif mac not in self._down:
                self._down.add(mac)
                dropped.append(mac)
        # with nothing left the caller fails the whole update and Home
        # Assistant logs that once; a warning per unit would only repeat it.
        # Such a unit is named later if it is still down once others answer.
        if not data:
            for mac in dropped:
                _LOGGER.debug("%s is unavailable: %s", self._name(mac), failed[mac])
            self._quiet.update(dropped)
            dropped = []
        else:
            dropped += [mac for mac in failed if mac in self._quiet]
            self._quiet.difference_update(dropped)
        for mac in dropped:
            _LOGGER.warning("%s is unavailable: %s", self._name(mac), failed[mac])
        return data

    def reached(self, mac: str) -> None:
        """The unit answered (a poll, or a command that got through)."""
        self._misses.pop(mac, None)
        self._quiet.discard(mac)
        if mac in self._down:
            self._down.discard(mac)
            _LOGGER.info("%s answers again", self._name(mac))

    def _name(self, mac: str) -> str:
        return self._names.get(mac) or "unit"
