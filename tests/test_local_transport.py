from uarb_agent.mail import LocalTransport, OutboundMessage


def test_roundtrip(tmp_path):
    t = LocalTransport(tmp_path / "mb", address="agent@test")
    m = t.drop("nathan@example.com", "M12205 exhibits", "please")
    pending = t.fetch_unprocessed()
    assert [p.id for p in pending] == [m.id]
    assert pending[0].subject == "M12205 exhibits"

    t.mark_processed(pending[0])
    assert t.fetch_unprocessed() == []

    att = tmp_path / "a.zip"
    att.write_bytes(b"PK")
    out_id = t.send(OutboundMessage(to="nathan@example.com", subject="re", body="done", attachments=[att]))
    sent = t.sent()
    assert sent[0]["id"] == out_id
    assert sent[0]["attachments"][0].endswith("a.zip")


def test_order_is_oldest_first(tmp_path):
    t = LocalTransport(tmp_path / "mb")
    a = t.drop("x", "first", "")
    b = t.drop("x", "second", "")
    import os, time
    os.utime(tmp_path / "mb" / "inbox" / f"{a.id}.json", (1, 1))
    assert [m.subject for m in t.fetch_unprocessed()] == ["first", "second"]
