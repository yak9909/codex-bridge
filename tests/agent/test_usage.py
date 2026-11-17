from codex_bridge.agent.usage import (STOP_TEXT, BlockNew, Interrupt, QuotaGuard, Steer, Suspend, Unblock,
                                     UsageSnapshot, Window, snapshot_from_rate_limits)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def snap(five=None, weekly=None, reset=1000):
    return UsageSnapshot(five_hour=None if five is None else Window(five, reset),
                         weekly=None if weekly is None else Window(weekly, reset))


def guard(clock=None):
    return QuotaGuard({"five_hour": 90, "weekly": 95}, clock=clock or FakeClock())


def test_windows_identified_by_duration_not_position():
    s = snapshot_from_rate_limits({"rateLimits": {
        "primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 2000},
        "secondary": {"usedPercent": 91, "windowDurationMins": 300, "resetsAt": 1000}}})
    assert s.five_hour == Window(91, 1000) and s.weekly == Window(40, 2000)


def test_prefers_codex_bucket_and_handles_missing():
    s = snapshot_from_rate_limits({"rateLimits": {"primary": None, "secondary": None},
                                   "rateLimitsByLimitId": {"codex": {
                                       "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 9}}}})
    assert s.five_hour == Window(5, 9) and s.weekly is None


def test_threshold_steers_once_during_turn():
    g = guard()
    g.on_turn_started("u1")
    assert g.on_usage(snap(five=91)) == [Steer(STOP_TEXT), BlockNew(1000)]
    assert g.on_usage(snap(five=92)) == []


def test_weekly_threshold_also_triggers():
    g = guard()
    g.on_turn_started("u1")
    assert g.on_usage(snap(five=10, weekly=96, reset=5000)) == [Steer(STOP_TEXT), BlockNew(5000)]


def test_suspend_after_turn_completes():
    g = guard()
    g.on_turn_started("u1")
    g.on_usage(snap(five=91))
    assert g.on_turn_completed() == [Suspend(1000)]


def test_interrupt_after_grace():
    clock = FakeClock()
    g = guard(clock)
    g.on_turn_started("u1")
    g.on_usage(snap(five=91))
    clock.advance(599)
    assert g.tick() == []
    clock.advance(2)
    assert g.tick() == [Interrupt(), Suspend(1000)]
    assert g.on_turn_completed() == []


def test_no_active_turn_only_blocks():
    g = guard()
    assert g.on_usage(snap(five=91)) == [BlockNew(1000)]


def test_steer_failure_falls_back_to_next_turn_start():
    g = guard()
    g.on_turn_started("u1")
    g.on_usage(snap(five=91))
    g.on_steer_failed()
    g.on_turn_completed()
    assert g.on_turn_started("u2") == [Steer(STOP_TEXT)]


def test_turn_started_while_blocked_gets_stop_instruction():
    g = guard()
    g.on_usage(snap(five=91))
    assert g.on_turn_started("u1") == [Steer(STOP_TEXT)]


def test_unblock_when_usage_drops():
    g = guard()
    g.on_usage(snap(five=91))
    assert g.on_usage(snap(five=10, reset=5000)) == [Unblock()]
    assert g.on_usage(snap(five=10, reset=5000)) == []


def test_disabled_guard_does_nothing():
    g = guard()
    g.enabled = False
    g.on_turn_started("u1")
    assert g.on_usage(snap(five=99)) == []


def test_threshold_change_applies():
    g = guard()
    g.set_thresholds({"five_hour": 95, "weekly": 95})
    assert g.on_usage(snap(five=91)) == []


def test_retry_cleared_when_usage_recovers():
    # Review I7: a failed steer must not stop the first task after the window resets.
    g = guard()
    g.on_turn_started("u1")
    g.on_usage(snap(five=91))
    g.on_steer_failed()
    g.on_turn_completed()
    assert g.on_usage(snap(five=10, reset=5000)) == [Unblock()]
    assert g.on_turn_started("u2") == []


def test_no_suspend_with_zero_resume_time():
    g = guard()
    g.on_turn_started("u1")
    g.on_steer_failed()  # retry flag without any block
    actions = g.on_turn_started("u2")
    assert all(not (isinstance(a, Suspend) and a.resume_at == 0) for a in actions + g.on_turn_completed())


def test_friend_limit_lower_than_host_wins():
    g = guard()                                   # host: 90 / 95
    g.set_local_limits({"five_hour": 50, "weekly": None}, force=False)
    g.on_turn_started("u1")
    assert g.on_usage(snap(five=60)) == [Steer(STOP_TEXT), BlockNew(1000)]


def test_friend_limit_higher_than_host_does_not_loosen():
    g = guard()
    g.set_local_limits({"five_hour": 99, "weekly": 99}, force=False)
    assert g.on_usage(snap(five=91)) == [BlockNew(1000)]


def test_force_stop_interrupts_at_once():
    g = guard()
    g.set_local_limits({"five_hour": 50, "weekly": None}, force=True)
    g.on_turn_started("u1")
    assert g.on_usage(snap(five=60)) == [Interrupt(), BlockNew(1000)]
    assert g.on_turn_completed() == [Suspend(1000)]


def test_force_stop_also_applies_at_turn_start_while_blocked():
    g = guard()
    g.set_local_limits({"five_hour": 50, "weekly": None}, force=True)
    g.on_usage(snap(five=60))
    assert g.on_turn_started("u1") == [Interrupt()]


def test_effective_limits_reported():
    g = guard()
    g.set_local_limits({"five_hour": 50, "weekly": None}, force=True)
    assert g.effective_limits() == {"five_hour": 50, "weekly": 95}


def test_stop_text_says_stop_now_and_report():
    assert "今から中断処理に入ります" in STOP_TEXT
    assert "すぐに終了" in STOP_TEXT
    assert "区切り" not in STOP_TEXT
