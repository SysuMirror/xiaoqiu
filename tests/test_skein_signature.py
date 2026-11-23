import time

from app.common import skein_signature


def test_signature_roundtrip():
    body = b'{"event":"ticket.status"}'
    timestamp = str(int(time.time()))
    value = skein_signature.signature(body, timestamp, "whsec_k")
    assert value.startswith("v1=") and len(value) == 67
    assert skein_signature.verify(body, timestamp, value, "whsec_k")


def test_rejects_wrong_secret_body_timestamp_and_missing_parts():
    body = b'{"event":"ticket.status"}'
    timestamp = str(int(time.time()))
    value = skein_signature.signature(body, timestamp, "whsec_k")
    assert not skein_signature.verify(body, timestamp, value, "whsec_other")
    assert not skein_signature.verify(body + b" ", timestamp, value, "whsec_k")
    stale = str(int(time.time()) - skein_signature.TOLERANCE_SECONDS - 1)
    assert not skein_signature.verify(body, stale, skein_signature.signature(body, stale, "whsec_k"), "whsec_k")
    assert not skein_signature.verify(body, None, value, "whsec_k")
    assert not skein_signature.verify(body, timestamp, None, "whsec_k")
    assert not skein_signature.verify(body, timestamp, value, "")
    assert not skein_signature.verify(body, "not-a-number", value, "whsec_k")
