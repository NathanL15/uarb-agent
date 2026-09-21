from datetime import datetime, timedelta, timezone

from uarb_agent.mail.guards import RateLimiter, looks_automated, old_enough, too_old


def test_robot_senders_are_ignored():
    assert looks_automated("mailer-daemon@googlemail.com", {})
    assert looks_automated("noreply@example.com", {})
    assert looks_automated("no-reply@example.com", {})
    assert looks_automated("postmaster@example.com", {})
    assert looks_automated("", {})


def test_auto_submitted_headers():
    assert looks_automated("a@b.c", {"Auto-Submitted": "auto-replied"})
    assert looks_automated("a@b.c", {"Precedence": "bulk"})
    assert looks_automated("a@b.c", {"List-Unsubscribe": "<mailto:x>"})
    assert looks_automated("a@b.c", {}, "Automatic reply: M12205")
    assert looks_automated("a@b.c", {}, "Out of Office")
    assert looks_automated("a@b.c", {"Auto-Submitted": "no"}) is None
    assert looks_automated("nathan@example.com", {"Subject": "M12205 exhibits"}) is None


def test_rate_limiter_per_sender_and_global():
    t = [0.0]
    rl = RateLimiter(per_sender=2, global_limit=3, window_s=100, clock=lambda: t[0])
    assert rl.allow("a@x") and rl.allow("a@x")
    assert not rl.allow("a@x")
    assert rl.allow("b@x")
    assert not rl.allow("c@x")  # global cap
    t[0] = 200.0
    assert rl.allow("a@x")


def test_age_helpers():
    now = datetime.now(timezone.utc)
    assert too_old(now - timedelta(days=4))
    assert not too_old(now - timedelta(hours=1))
    assert old_enough(now - timedelta(minutes=5), 180)
    assert not old_enough(now - timedelta(seconds=30), 180)
    assert old_enough(now, 0)
