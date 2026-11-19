"""重放防禦：時間窗、開機門檻、已見 ID 與過期清除。"""

import pytest

from llmop.protocol import PROTOCOL_VERSION, REQUEST_TYP, Header, ProtocolError, b64u_encode, new_request_id
from llmop.worker.replay import ReplayGuard

START = 1_800_000_000
SIG_A = b64u_encode(b"a" * 16)
SIG_B = b64u_encode(b"b" * 16)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def header(ts, request_id=None, sig_key_id=SIG_A):
    return Header(
        v=PROTOCOL_VERSION,
        typ=REQUEST_TYP,
        request_id=request_id or new_request_id(),
        ts=ts,
        key_id=b64u_encode(b"k" * 16),
        sig_key_id=sig_key_id,
        model="m",
        max_tokens=1,
    )


def code_of(guard, h):
    with pytest.raises(ProtocolError) as info:
        guard.check(h)
    return info.value.code


def test_window_accepts_edges_and_rejects_beyond():
    clock = Clock(START)
    guard = ReplayGuard(clock)
    clock.now = START + 1000
    guard.check(header(START + 1000 - 300))
    guard.check(header(START + 1000 + 300))
    assert code_of(guard, header(START + 1000 - 301)) == "stale_timestamp"
    assert code_of(guard, header(START + 1000 + 301)) == "stale_timestamp"


def test_boot_barrier_rejects_requests_signed_before_start():
    clock = Clock(START)
    guard = ReplayGuard(clock)
    clock.now = START + 10
    assert code_of(guard, header(START - 1)) == "stale_timestamp"  # 仍在時間窗內，但早於啟動
    guard.check(header(START))


def test_replayed_request_id_is_rejected_per_signer():
    clock = Clock(START)
    guard = ReplayGuard(clock)
    request_id = new_request_id()
    guard.check(header(START, request_id))
    assert code_of(guard, header(START, request_id)) == "replayed"
    guard.check(header(START, request_id, sig_key_id=SIG_B))  # 不同簽署者的相同 ID 是另一筆


def test_seen_entry_lives_until_ts_plus_window_even_with_fractional_clock():
    clock = Clock(START)
    guard = ReplayGuard(clock)
    request_id = new_request_id()
    guard.check(header(START + 5, request_id))
    clock.now = START + 5 + 300.9  # 整數秒仍在時間窗內
    assert code_of(guard, header(START + 5, request_id)) == "replayed"


def test_expired_entries_are_purged_on_insert():
    clock = Clock(START)
    guard = ReplayGuard(clock)
    for _ in range(3):
        guard.check(header(START))
    assert len(guard) == 3
    clock.now = START + 301
    guard.check(header(START + 301))
    assert len(guard) == 1
