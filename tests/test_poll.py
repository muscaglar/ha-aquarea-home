"""One poll pass over several units, and the per-unit grace bookkeeping."""
import logging

import pytest

from custom_components.aquarea_home import api, poll

UNITS = [{"mac": m, "name": n, "node_id": 0}
         for m, n in (("A", "Lounge"), ("B", "Bedroom"), ("C", "Office"), ("D", "Loft"))]
NAMES = {d["mac"]: d["name"] for d in UNITS}
ON = {"power": True}


class FakeClient:
    """get_state answers from a per-MAC script: a dict, or an exception to raise."""

    def __init__(self, script: dict) -> None:
        self.script = script
        self.asked: list[str] = []

    async def get_state(self, mac: str, node_id: int = 0) -> dict:
        self.asked.append(mac)
        answer = self.script.get(mac, ON)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


# ---------------------------------------------------------------------------
# poll_units
# ---------------------------------------------------------------------------

async def test_one_unreachable_unit_does_not_stop_the_pass():
    client = FakeClient({"B": api.DeviceOffline("RESPONSE_TIMEOUT")})
    fresh, failed = await poll.poll_units(client, UNITS)
    assert sorted(fresh) == ["A", "C", "D"]
    assert list(failed) == ["B"]
    assert client.asked == ["A", "B", "C", "D"]


async def test_a_broken_path_to_the_cloud_ends_the_pass_early():
    boom = api.AquareaHomeError("gRPC transport error: [Errno -3] Try again")
    client = FakeClient({"B": boom})
    fresh, failed = await poll.poll_units(client, UNITS)
    assert list(fresh) == ["A"]
    assert failed == {"B": boom, "C": boom, "D": boom}
    assert client.asked == ["A", "B"]          # no 20 s timeout per remaining unit


async def test_auth_error_is_the_callers_business():
    client = FakeClient({"A": api.AuthError("UNAUTHENTICATED")})
    with pytest.raises(api.AuthError):
        await poll.poll_units(client, UNITS)


async def test_one_forbidden_unit_is_just_that_unit():
    client = FakeClient({"C": api.UnitForbidden("PERMISSION_DENIED")})
    fresh, failed = await poll.poll_units(client, UNITS)
    assert sorted(fresh) == ["A", "B", "D"] and list(failed) == ["C"]


async def test_every_unit_forbidden_is_reported_as_it_is():
    refused = {d["mac"]: api.UnitForbidden("PERMISSION_DENIED") for d in UNITS}
    fresh, failed = await poll.poll_units(FakeClient(refused), UNITS)
    assert not fresh and sorted(failed) == ["A", "B", "C", "D"]
    # whether that means "try a fresh token" is the coordinator's call
    assert poll.all_forbidden(fresh, failed)
    assert not poll.all_forbidden({"A": ON}, {"B": refused["B"]})
    assert not poll.all_forbidden({}, {"A": refused["A"], "B": api.DeviceOffline("OFFLINE")})
    assert not poll.all_forbidden({}, {})


async def test_timeouts_with_nothing_answering_end_the_pass():
    slow = api.RequestTimeout("no reply within 20 s")
    client = FakeClient({d["mac"]: slow for d in UNITS})
    fresh, failed = await poll.poll_units(client, UNITS, answered_last={"A", "B", "C", "D"})
    assert not fresh and sorted(failed) == ["A", "B", "C", "D"]
    assert client.asked == ["A", "B"]


async def test_with_no_history_every_unit_is_asked():
    # first pass: two slow units listed first must not stand for the cloud
    slow = api.RequestTimeout("no reply within 20 s")
    client = FakeClient({"A": slow, "B": slow})
    fresh, failed = await poll.poll_units(client, UNITS)
    assert sorted(fresh) == ["C", "D"] and sorted(failed) == ["A", "B"]
    assert client.asked == ["A", "B", "C", "D"]


async def test_units_that_answered_last_time_are_asked_first():
    slow = api.RequestTimeout("no reply within 20 s")
    client = FakeClient({"A": slow, "B": slow})
    fresh, failed = await poll.poll_units(client, UNITS, answered_last={"C", "D"})
    assert client.asked == ["C", "D", "A", "B"]
    assert sorted(fresh) == ["C", "D"] and sorted(failed) == ["A", "B"]


async def test_an_offline_answer_is_still_the_cloud_answering():
    slow = api.RequestTimeout("no reply within 20 s")
    client = FakeClient({"A": api.DeviceOffline("OFFLINE"), "B": slow, "C": slow})
    fresh, failed = await poll.poll_units(client, UNITS, answered_last={"A", "B", "C", "D"})
    assert client.asked == ["A", "B", "C", "D"] and list(fresh) == ["D"]


async def test_a_slow_unit_among_healthy_ones_is_just_that_unit():
    slow = api.RequestTimeout("no reply within 20 s")
    client = FakeClient({"B": slow, "C": slow})
    fresh, failed = await poll.poll_units(client, UNITS, answered_last={"A", "D"})
    assert sorted(fresh) == ["A", "D"] and sorted(failed) == ["B", "C"]


async def test_an_unreadable_reply_costs_only_that_unit():
    client = FakeClient({"B": api.BadReply("unreadable get_state reply"),
                         "C": api.UnitError("gRPC INTERNAL: oops")})
    fresh, failed = await poll.poll_units(client, UNITS)
    assert sorted(fresh) == ["A", "D"] and client.asked == ["A", "B", "C", "D"]


async def test_no_units_is_not_an_error():
    assert await poll.poll_units(FakeClient({}), []) == ({}, {})


# ---------------------------------------------------------------------------
# UnitBook
# ---------------------------------------------------------------------------

def test_a_unit_rides_out_the_grace_on_its_last_state_then_drops_alone():
    book = poll.UnitBook(NAMES, grace=3)
    last = {"A": {"power": True, "setpoint": 21.0}, "B": ON}
    err = {"A": api.DeviceOffline("RESPONSE_TIMEOUT")}
    data = book.settle(last, {"B": ON}, err)
    assert data["A"] == last["A"]                       # miss 1: last state
    data = book.settle(data, {"B": ON}, err)
    assert data["A"] == last["A"]                       # miss 2: last state
    data = book.settle(data, {"B": ON}, err)
    assert "A" not in data and data["B"] == ON          # miss 3: A only
    data = book.settle(data, {"B": ON}, err)
    assert "A" not in data


def test_a_unit_never_seen_has_nothing_to_ride_on():
    book = poll.UnitBook(NAMES, grace=3)
    data = book.settle({}, {"B": ON}, {"A": api.DeviceOffline("OFFLINE")})
    assert data == {"B": ON}                            # setup still succeeds for B


def test_recovery_restarts_the_grace():
    book = poll.UnitBook(NAMES, grace=3)
    err = {"A": api.DeviceOffline("x")}
    data = book.settle({"A": ON}, {}, err)
    data = book.settle(data, {}, err)
    data = book.settle(data, {"A": ON}, {})             # back before the third miss
    data = book.settle(data, {}, err)
    assert "A" in data                                  # counts from one again


def test_a_command_that_got_through_counts_as_an_answer():
    book = poll.UnitBook(NAMES, grace=2)
    data = book.settle({"A": ON}, {}, {"A": api.DeviceOffline("x")})
    book.reached("A")
    data = book.settle(data, {}, {"A": api.DeviceOffline("x")})
    assert "A" in data


def test_whole_account_outage_leaves_nothing_after_the_grace():
    book = poll.UnitBook(NAMES, grace=3)
    boom = api.AquareaHomeError("transport")
    data = {d["mac"]: ON for d in UNITS}
    for _ in range(2):
        data = book.settle(data, {}, {d["mac"]: boom for d in UNITS})
        assert len(data) == 4
    assert book.settle(data, {}, {d["mac"]: boom for d in UNITS}) == {}


def test_a_dropped_unit_is_named_once_at_warning(caplog):
    book = poll.UnitBook(NAMES, grace=1)
    err = {"A": api.DeviceOffline("RESPONSE_TIMEOUT")}
    with caplog.at_level(logging.DEBUG):
        book.settle({"A": ON, "B": ON}, {"B": ON}, err)
        book.settle({"B": ON}, {"B": ON}, err)
        book.settle({"B": ON}, {"A": ON, "B": ON}, {})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Lounge" in warnings[0].getMessage()
    assert "RESPONSE_TIMEOUT" in warnings[0].getMessage()
    assert any("Lounge answers again" in r.getMessage() for r in caplog.records)


def test_no_per_unit_warning_when_the_whole_update_fails_anyway(caplog):
    book = poll.UnitBook(NAMES, grace=1)
    with caplog.at_level(logging.DEBUG):
        data = book.settle({"A": ON}, {}, {"A": api.AquareaHomeError("transport")})
    assert data == {}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_unit_still_down_after_an_account_outage_is_named_once_others_answer(caplog):
    book = poll.UnitBook(NAMES, grace=1)
    boom = {d["mac"]: api.AquareaHomeError("transport") for d in UNITS}
    offline = {"B": api.DeviceOffline("RESPONSE_TIMEOUT")}
    back = {mac: ON for mac in "ACD"}
    with caplog.at_level(logging.WARNING):
        assert book.settle({d["mac"]: ON for d in UNITS}, {}, boom) == {}
        assert not caplog.records                       # the caller logs the outage
        book.settle({}, back, offline)
        book.settle(back, back, offline)
    warnings = [r.getMessage() for r in caplog.records]
    assert warnings == ["Bedroom is unavailable: RESPONSE_TIMEOUT"]

