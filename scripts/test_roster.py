"""Character picker (leader/supporter selection): the UI's `assign` message puts roster
characters into the a(leader)/b(supporter) slots and connects. Guards: roster names only
ever leave the server (never passwords), assignment is refused while anything is
connected, and leader/supporter must be two different roster characters."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.webui import WebUI


class _StubSession:
    def __init__(self, sid, name, password):
        self.sid, self.name, self.password = sid, name, password
        self.status = "disconnected"
        self.partner_name = None
        self.started = False
        self.emitted = 0

    def _emit_state(self):
        self.emitted += 1

    async def start(self):
        self.started = True


def _ui():
    a = _StubSession("a", "현실의자각", "pw-a")
    b = _StubSession("b", "스튀르들뤼손", "pw-b")
    ui = WebUI({"a": a, "b": b}, ui_port=0)
    ui.roster = [{"name": "현실의자각", "password": "pw-a"},
                 {"name": "스튀르들뤼손", "password": "pw-b"},
                 {"name": "플레이어제로", "password": "pw-c"}]
    sent = []
    ui.broadcast = lambda msg: sent.append(msg)     # capture instead of ws fan-out
    return ui, a, b, sent


def test_roster_msg_never_leaks_passwords():
    ui, a, b, _ = _ui()
    msg = ui._roster_msg()
    assert msg["names"] == ["현실의자각", "스튀르들뤼손", "플레이어제로"]
    assert msg["leader"] == "현실의자각" and msg["supporter"] == "스튀르들뤼손"
    blob = json.dumps(msg, ensure_ascii=False)
    assert "pw-" not in blob and "password" not in blob, f"password leaked: {blob}"
    print("roster msg: names + assignment only — no password ever leaves the server")


def test_assign_swaps_slots_and_connects():
    ui, a, b, sent = _ui()

    async def run():
        await ui.dispatch({"type": "assign", "leader": "플레이어제로",
                           "supporter": "현실의자각", "connect": True})
        await asyncio.sleep(0)                       # let ensure_future(start) run

    asyncio.run(run())
    assert a.name == "플레이어제로" and a.password == "pw-c", (a.name, a.password)
    assert b.name == "현실의자각" and b.password == "pw-a"
    assert a.partner_name == b.name and b.partner_name == a.name, "partner links must cross"
    assert a.started and b.started, "connect:true must start both sessions"
    assert a.emitted and b.emitted, "panes must refresh (state re-emit) after assignment"
    rosters = [m for m in sent if m.get("type") == "roster"]
    assert rosters and rosters[-1]["leader"] == "플레이어제로", "assignment must be re-broadcast"
    assert "pw-" not in json.dumps(sent, ensure_ascii=False), "a broadcast leaked a password"
    print("assign: slots swapped, partners crossed, both connected, roster re-broadcast")


def test_assign_refused_while_connected():
    ui, a, b, sent = _ui()
    a.status = "online"                              # anything non-disconnected blocks it
    asyncio.run(ui.dispatch({"type": "assign", "leader": "플레이어제로",
                             "supporter": "스튀르들뤼손"}))
    assert a.name == "현실의자각" and b.name == "스튀르들뤼손", "assignment must not change live sessions"
    assert not a.started and not b.started
    assert any(m.get("type") == "error" for m in sent), "must surface the refusal"
    print("assign: refused while any session is connected")


def test_assign_rejects_same_or_unknown():
    ui, a, b, sent = _ui()
    asyncio.run(ui.dispatch({"type": "assign", "leader": "플레이어제로",
                             "supporter": "플레이어제로"}))
    assert a.name == "현실의자각" and any(m.get("type") == "error" for m in sent)
    sent.clear()
    asyncio.run(ui.dispatch({"type": "assign", "leader": "없는캐릭",
                             "supporter": "스튀르들뤼손"}))
    assert a.name == "현실의자각" and any(m.get("type") == "error" for m in sent)
    assert not a.started and not b.started
    print("assign: same-character and unknown-character selections are rejected")


def test_sessions_msg_layout_no_passwords():
    ui, a, b, _ = _ui()
    msg = ui._sessions_msg()
    assert [e["sid"] for e in msg["list"]] == ["a", "b"]
    assert [e["role"] for e in msg["list"]] == ["leader", "supporter"]
    assert "pw-" not in json.dumps(msg, ensure_ascii=False)
    print("sessions msg: sid/role/name/status layout, no passwords")


def test_add_supporter_creates_reuses_refuses():
    ui, a, b, sent = _ui()
    made = []

    def factory(name, password):
        s = _StubSession("c", name, password)
        ui.sessions["c"] = s
        made.append(s)
        return s

    ui.new_supporter = factory

    async def run(msg):
        await ui.dispatch(msg)
        await asyncio.sleep(0)

    # a NEW character -> factory creates sid c, partner = leader, connected
    asyncio.run(run({"type": "add_supporter", "name": "플레이어제로"}))
    assert made and made[0].name == "플레이어제로" and made[0].password == "pw-c"
    assert made[0].partner_name == a.name, "extra supporter follows the LEADER"
    assert made[0].started, "added supporter must connect"
    assert any(m.get("type") == "sessions" for m in sent), "layout change must broadcast"
    assert "pw-" not in json.dumps(sent, ensure_ascii=False)

    # a character already holding a DISCONNECTED session -> reused, not duplicated
    sent.clear(); made.clear()
    asyncio.run(run({"type": "add_supporter", "name": "스튀르들뤼손"}))
    assert not made, "existing disconnected session must be reused, not re-created"
    assert b.started, "reused session must connect"

    # already CONNECTED character -> refused
    sent.clear()
    ui.sessions["c"].status = "online"
    asyncio.run(run({"type": "add_supporter", "name": "플레이어제로"}))
    assert any(m.get("type") == "error" for m in sent), "connected char must be refused"

    # unknown character -> refused
    sent.clear()
    asyncio.run(run({"type": "add_supporter", "name": "없는캐릭"}))
    assert any(m.get("type") == "error" for m in sent)
    print("add_supporter: creates (partner=leader) / reuses disconnected / refuses live+unknown")


def test_relink_group_reflects_connected_supporters():
    """WebUI.relink_group wires the leader's `partners` from the CONNECTED sessions only —
    number-agnostic, and a disconnected supporter drops out (never waited on)."""
    class _Ctx:
        def __init__(self, sid, role):
            self.sid, self.role = sid, role
            self.partner = None
            self.partners = []
    ui, a, b, _ = _ui()
    a.ctx = _Ctx("a", "leader"); a.status = "online"
    b.ctx = _Ctx("b", "supporter"); b.status = "online"
    c = _StubSession("c", "플레이어제로", "pw-c"); c.ctx = _Ctx("c", "supporter"); c.status = "online"
    ui.sessions["c"] = c

    ui.relink_group()
    assert set(id(x) for x in a.ctx.partners) == {id(b.ctx), id(c.ctx)}, "leader sees BOTH supporters"
    assert a.ctx.partner is b.ctx, "compat single partner = first supporter"
    assert b.ctx.partner is a.ctx and c.ctx.partner is a.ctx, "each supporter -> leader"

    # a supporter disconnects -> it drops out of the leader's group on the next relink
    c.status = "disconnected"
    ui.relink_group()
    assert [id(x) for x in a.ctx.partners] == [id(b.ctx)], "dropped supporter falls out of partners"
    print("relink: leader.partners = connected supporters; a disconnect drops one out")


def test_group_toggle_flips_flag_and_leader_issues_command():
    """The group checkbox: toggling a supporter flips its in_group flag and, when the leader is
    online, the LEADER issues the '<name> 그룹' toggle live. Leader/self sid has no checkbox."""
    class _Ctx:
        def __init__(self): self.in_group = True
    class _Sender:
        def __init__(self): self.pushed = []
        def push(self, cmd): self.pushed.append(cmd)
    ui, a, b, sent = _ui()
    b.ctx = _Ctx(); a.status = "online"; a.sender = _Sender()

    asyncio.run(ui.dispatch({"type": "group_toggle", "sid": "b"}))
    assert b.ctx.in_group is False, "unchecking excludes the supporter"
    assert a.sender.pushed == ["스튀르들뤼손 그룹"], "leader issues the toggle by NAME"
    asyncio.run(ui.dispatch({"type": "group_toggle", "sid": "b"}))
    assert b.ctx.in_group is True, "re-checking includes it again"
    assert a.sender.pushed == ["스튀르들뤼손 그룹", "스튀르들뤼손 그룹"]

    a.ctx = _Ctx()
    asyncio.run(ui.dispatch({"type": "group_toggle", "sid": "a"}))
    assert a.ctx.in_group is True, "the leader has no group checkbox — ignored"
    print("group_toggle: flips the supporter flag + leader issues '<name> 그룹'; leader sid ignored")


if __name__ == "__main__":
    test_roster_msg_never_leaks_passwords()
    test_relink_group_reflects_connected_supporters()
    test_group_toggle_flips_flag_and_leader_issues_command()
    test_assign_swaps_slots_and_connects()
    test_assign_refused_while_connected()
    test_assign_rejects_same_or_unknown()
    test_sessions_msg_layout_no_passwords()
    test_add_supporter_creates_reuses_refuses()
    print("\nALL ROSTER TESTS PASSED")
