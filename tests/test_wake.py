import hashlib
import hmac

from fulla.signature import verify

SECRET = "whsec_test"
BODY = b'{"type":"wake","worker_id":"fulla"}'


def sign(secret: str, t: int, body: bytes = BODY) -> str:
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def test_good_signature():
    assert verify(SECRET, BODY, sign(SECRET, 1_760_572_800), now=1_760_572_800)


def test_bad_mac():
    assert not verify(SECRET, BODY, sign("other-secret", 1_760_572_800), now=1_760_572_800)


def test_body_tamper():
    assert not verify(SECRET, b'{"type":"wake","worker_id":"mimir"}', sign(SECRET, 1_760_572_800),
                      now=1_760_572_800)


def test_stale_timestamp():
    t = 1_760_572_800
    assert verify(SECRET, BODY, sign(SECRET, t), now=t + 299)
    assert not verify(SECRET, BODY, sign(SECRET, t), now=t + 301)
    assert not verify(SECRET, BODY, sign(SECRET, t), now=t - 301)


def test_malformed_header():
    assert not verify(SECRET, BODY, "")
    assert not verify(SECRET, BODY, "v1=abc")
    assert not verify(SECRET, BODY, "t=notanumber,v1=abc")
    assert not verify(SECRET, BODY, "t=\u00b2,v1=abc")  # a digit to isdigit, not to int


def test_a_timestamp_past_the_int_string_limit_is_refused_not_raised():
    assert not verify(SECRET, BODY, f"t={'1' * 5000},v1=abc", now=1_760_572_800)
    assert not verify(SECRET, BODY, f"t={'0' * 13},v1=abc", now=0)  # too long, though zero


def test_a_non_ascii_mac_is_refused_not_raised():
    t = 1_760_572_800
    mac = sign(SECRET, t).split("v1=", 1)[1]
    assert not verify(SECRET, BODY, f"t={t},v1=\u00e9", now=t)
    assert not verify(SECRET, BODY, f"t={t},v1={mac[:-1]}\u00e9", now=t)


def test_other_malformed_headers_are_refused():
    t = 1_760_572_800
    mac = sign(SECRET, t).split("v1=", 1)[1]
    for header in [
        "garbage", ",,,", "=", "t=,v1=", f"t={t}", f"t={t},v1=", f"t=,v1={mac}",
        f"t=-{t},v1={mac}", f"t= {t},v1={mac}", f"t={t}.0,v1={mac}", f"t={t},v1={mac[:-2]}",
        f"t={t},v1={mac.upper()}", f"t={t},v1={mac}\x00", "t=\u0661\u0662,v1=abc",
    ]:
        assert not verify(SECRET, BODY, header, now=t), header


def test_telegram_secret_token_is_derived_and_telegram_safe():
    from fulla.signature import telegram_secret_token

    a = telegram_secret_token("s1")
    assert a == telegram_secret_token("s1")
    assert a != telegram_secret_token("s2")
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_an_empty_secret_never_verifies():
    now = 1_800_000_000
    assert not verify("", BODY, sign("", now), now=now)
