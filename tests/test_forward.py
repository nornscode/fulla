"""The Signal forwarder's spool: nothing dropped, delivered in order."""

import importlib.util
import threading
from pathlib import Path

FORWARD = Path(__file__).resolve().parents[1] / "deploy" / "signal" / "forward.py"


def load():
    spec = importlib.util.spec_from_file_location("forward", FORWARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_deliver(forward, spool, send, *, until):
    wake, stop = threading.Event(), threading.Event()
    t = threading.Thread(target=forward.deliver, args=(spool, send, wake, stop), kwargs={"backoff": 0.01})
    t.start()
    wake.set()
    until.wait(timeout=5)
    stop.set()
    wake.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_spool_delivers_oldest_first_and_removes_what_was_accepted(tmp_path):
    forward = load()
    spool = forward.Spool(tmp_path / "spool")
    for body in (b"one", b"two", b"three"):
        spool.put(body)
    assert len(spool.waiting()) == 3

    got, done = [], threading.Event()

    def send(body):
        got.append(body)
        if len(got) == 3:
            done.set()
        return True

    run_deliver(forward, spool, send, until=done)
    assert got == [b"one", b"two", b"three"]
    assert spool.waiting() == []


def test_a_message_fulla_will_not_take_holds_the_ones_behind_it(tmp_path):
    forward = load()
    spool = forward.Spool(tmp_path / "spool")
    spool.put(b"first")
    spool.put(b"second")

    attempts, done = [], threading.Event()

    def send(body):
        attempts.append(body)
        if body == b"first" and attempts.count(b"first") < 3:
            return False  # Fulla's machine still starting
        if body == b"second":
            done.set()
        return True

    run_deliver(forward, spool, send, until=done)
    assert attempts == [b"first", b"first", b"first", b"second"]
    assert spool.waiting() == []


def test_spool_survives_a_restart_and_ignores_half_written_files(tmp_path):
    forward = load()
    spool = forward.Spool(tmp_path / "spool")
    spool.put(b"kept")
    (tmp_path / "spool" / "99999999999999999999-000000.json.tmp").write_bytes(b"half")
    again = forward.Spool(tmp_path / "spool")
    assert [p.read_bytes() for p in again.waiting()] == [b"kept"]
    assert list((tmp_path / "spool").glob("*.tmp")) == []


def test_only_data_messages_are_interesting():
    forward = load()
    assert forward.interesting({"envelope": {"dataMessage": {"message": "hi"}}})
    assert not forward.interesting({"envelope": {"receiptMessage": {}}})
    assert not forward.interesting({"envelope": {"typingMessage": {}}})
    assert not forward.interesting({})
