"""Regression: the 따라 (follow) flag must be set ONLY from the follower's own
first-person line ("당신은 …를 따라다니기 시작"), matched LINE-BY-LINE — never from a
plain substring over a multi-line packet.

The live bug: a single TCP packet carried the 성의 일층 room description
("…당신은 무서움에 몸을 떨며…") AND the LEADER's third-person sighting of the
supporter following it ("스튀르들뤼손이 당신을 따라다니기 시작했습니다"). A
`"당신은" in t and "따라다니기 시작" in t` check over the whole packet then set the
LEADER's following=True — which made both_routes_done short-circuit, route_done fire
before travel walked a single step, and the party ping-pong 모두그룹/따라 at 중앙 광장."""

import re

# The exact patterns used in webui.on_text (line-anchored, first-person only).
START = re.compile(r"\s*당신은 .+[를을] 따라다니(기 시작|고 있습니다)")
STOP = re.compile(r"\s*당신은 .+따라다니는 것을 그만")


def _following_after(packet, initial=False):
    """Replicate webui.on_text's follow tracking over a raw multi-line packet."""
    following = initial
    for ln in packet.splitlines():
        if START.match(ln):
            following = True
        elif STOP.match(ln):
            following = False
    return following


# The real leader packet that used to false-trigger (room desc + 3rd-person follow).
LEADER_PACKET = (
    "\r\n어둠고 음침하기 이를데 없는 복도.........\r\n"
    "당신은 무서움에 몸을 떨며 돌아다녀보지만..\r\n"
    "당신은 궁금함을 참지 못하고 이층으로 가는 길을 찾아 헤맨다.\r\n"
    "[ 출구: 북 동 남 서 위 ]\r\n"
    "3247:100:405> \r\n"
    "스튀르들뤼손이 당신을 따라다니기 시작했습니다.\r\n"
)
# The real supporter packet (its own first-person follow).
SUP_START = "\r\n당신은 플레이어제로를 따라다니기 시작합니다.\r\n968:1382:250> "
SUP_KEEP = "\r\n당신은 이미 그를 따라다니고 있습니다.\r\n968:1382:250> "
SUP_STOP = "\r\n당신은 플레이어제로를 따라다니는 것을 그만둡니다.\r\n968:1382:250> "


def test_leader_third_person_follow_does_not_set_following():
    assert _following_after(LEADER_PACKET) is False, \
        "leader's 3rd-person '…이 당신을 따라…' + a room 당신은 must NOT set following"
    print("leader: room-desc 당신은 + '스튀르…이 당신을 따라다니기 시작' -> following stays False")


def test_supporter_first_person_sets_following():
    assert _following_after(SUP_START) is True, "follower's own line sets following"
    assert _following_after(SUP_KEEP) is True, "'이미 …따라다니고 있습니다' keeps it"
    assert _following_after(SUP_STOP, initial=True) is False, "explicit 그만 clears it"
    print("supporter: first-person 따라 start/keep set True; 그만 clears")


def test_room_desc_alone_never_sets_following():
    room_only = ("\r\n당신은 무서움에 몸을 떨며 돌아다녀보지만..\r\n"
                 "당신은 궁금함을 참지 못하고 이층으로 가는 길을 찾아 헤맨다.\r\n")
    assert _following_after(room_only) is False, "a 당신은 room line alone must not match"
    print("room desc alone: no follow phrase -> following stays False")


if __name__ == "__main__":
    test_leader_third_person_follow_does_not_set_following()
    test_supporter_first_person_sets_following()
    test_room_desc_alone_never_sets_following()
    print("\nALL FOLLOWING TESTS PASSED")
