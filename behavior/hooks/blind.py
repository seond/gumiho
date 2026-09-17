"""Blind recovery. On "당신의 눈이 멀었습니다!" the parser sets state.blind and the arbiter
force-switches to the `blind` workflow (blind.toml). While blinded the ONLY action that works
is a recall, so: 귀환 to the anchor (중앙 광장) -> walk 동 to the 치료소 -> "장님치 부탁" to be
cured -> resume hunting via travel. The recall/rest steps reuse resupply/travel actions
(do_recall, retry_recall, lie_down) and guards (at_anchor, cant_recall, can_recall)."""

from gumiho.registry import action, guard, step


# --- blind recall: confirm by the ABSENCE of the fail line (blindness hides the room render) ----
@action("blind_recall")
def blind_recall(s, cmd):
    # While blind the room never renders ("눈앞이 캄캄합니다"), so we CANNOT confirm a recall by
    # seeing 중앙 광장 (at_anchor never fires). Instead: send 귀환 and treat the absence of the
    # "귀환 시도가 실패했습니다" line as success (blind_recalled). Skip if too drained — cant_recall
    # then routes us to a rest to regen MV first.
    if s.mv < s.knowledge.get("travel", {}).get("recall_min_mv", 30):
        return
    cmd.send("귀환")
    s.together = False
    s._recall_sent_at = s._now()


@step("blind_recall_watch")
def blind_recall_watch(s, cmd):
    # Re-send 귀환 ONLY if the last attempt FAILED (a fail line arrived after our send). Otherwise
    # wait — blind_recalled confirms success once the window passes with no fail. (Never spam 귀환
    # blindly: a successful recall gives no render, so blind re-sends would fire in town forever.)
    sent = getattr(s, "_recall_sent_at", -1e9)
    if s.recall_fail_at >= sent:                       # our attempt failed -> retry (throttled)
        rc = s.knowledge.get("recall", {})
        if s._now() - sent >= rc.get("retry_interval", 3.0) \
                and s.mv >= s.knowledge.get("travel", {}).get("recall_min_mv", 30):
            cmd.send("귀환")
            s._recall_sent_at = s._now()


@guard("blind_recalled")
def blind_recalled(s):
    # Recalled iff the confirm window has passed since our 귀환 AND no fail line came after it.
    sent = getattr(s, "_recall_sent_at", 0.0)
    if sent <= 0:
        return False
    confirm = s.knowledge.get("recall", {}).get("blind_confirm_wait", 3.0)
    return (s._now() - sent) >= confirm and s.recall_fail_at < sent


@action("go_clinic")
def go_clinic(s, cmd):
    # From 중앙 광장, EAST is the 치료소 (허준 선생) — the description says
    # "동쪽으로 치료소가 보입니다". One step 동 reaches the healer who cures blindness.
    cmd.send("동")


@action("cure_blind")
def cure_blind(s, cmd):
    # Ask the healer to cure the blindness. This is the only recovery the user identified.
    cmd.send("장님치 부탁")


@action("clear_blind")
def clear_blind(s, cmd):
    # Cured -> clear the flag so the arbiter stops force-entering `blind` and lets us hunt.
    # (Optimistic: we don't yet know the cure-CONFIRMATION line, so we clear after the request;
    # if a stray blind lingers, the next "당신의 눈이 멀었습니다!" re-triggers the whole recovery.)
    s.state.blind = False


@guard("not_blind")
def not_blind(s):
    return not s.state.blind
