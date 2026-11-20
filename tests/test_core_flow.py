"""Functional tests of the collaboration round trip and human oversight."""
import pytest

from commons.errors import Conflict, Forbidden
from commons.simulator import BANNER, Simulator


def submit(e, **kw):
    args = dict(title="Review the brief", brief="Please critique the attached project brief.",
                info_category="project")
    args.update(kw)
    return e.core.submit_task(e.p_gpt, e.room["id"], e.claude["id"], **args)


def test_full_round_trip(env):
    e = env
    t = submit(e, idempotency_key="k1")
    assert t["state"] == "delivered"
    # Delivered is not retrieved.
    d = e.db.one("SELECT * FROM deliveries WHERE channel='inbox' AND recipient=?", (e.claude["id"],))
    assert d["status"] == "delivered" and d["retrieved_at"] is None

    inbox = e.core.read_inbox(e.p_claude)
    assert len(inbox["items"]) == 1
    item = inbox["items"][0]
    assert item["from"]["type"] == "agent" and item["from"]["represents"] == "Rafael Trava de Alba"
    assert "UNTRUSTED" in inbox["notice"]
    assert e.core._task_row(t["id"])["state"] == "retrieved"
    assert e.core.read_inbox(e.p_claude)["items"] == []  # not delivered twice

    r = e.core.respond(e.p_claude, t["id"], "Independent analysis: the scope is too broad.")
    assert r["task"]["state"] == "result_ready"

    got = e.core.read_inbox(e.p_gpt)["items"]
    assert [i["kind"] for i in got] == ["response"]
    e.core.respond(e.p_gpt, t["id"], "Synthesis: narrow the scope to one market.", final=True)

    assert e.core.accept_task(e.owner, t["id"])["state"] == "accepted"
    states = [r["to_state"] for r in e.db.all("SELECT to_state FROM task_transitions WHERE task_id=? ORDER BY id", (t["id"],))]
    assert states == ["created", "authorized", "queued", "delivered", "retrieved", "result_ready", "accepted"]
    assert e.core.audit.verify()["valid"]


def test_telegram_delivery_recorded_separately_and_honestly(env):
    e = env
    t = submit(e)
    tg = e.db.all("SELECT d.* FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.task_id=? AND d.channel='telegram'", (t["id"],))
    # Room has no Telegram chat: recorded as skipped, never as delivered.
    assert tg and all(d["status"] == "skipped" for d in tg)
    e.core.update_room(e.owner, e.room["id"], telegram_chat_id="-100123")
    t2 = submit(e, title="Second", brief="Another brief.")
    tg2 = e.db.all("SELECT d.* FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.task_id=? AND d.channel='telegram'", (t2["id"],))
    assert any(d["status"] == "pending" for d in tg2)
    # A Telegram post never marks the task as retrieved.
    assert e.core._task_row(t2["id"])["state"] == "delivered"


def test_out_of_grant_task_needs_owner_approval_then_delivers(env):
    e = env
    t = submit(e, info_category="schedule")  # grant covers general+project only
    assert t["state"] == "awaiting_authorization"
    assert e.core.read_inbox(e.p_claude)["items"] == []  # nothing delivered before approval
    appr = e.core.approvals_for_owner(e.owner)
    assert len(appr) == 1 and appr[0]["kind"] == "task_authorization"
    e.core.decide_approval(e.owner, appr[0]["id"], approve=True)
    assert e.core._task_row(t["id"])["state"] == "delivered"
    assert len(e.core.read_inbox(e.p_claude)["items"]) == 1


def test_owner_denial_cancels(env):
    e = env
    t = submit(e, info_category="schedule")
    a = e.core.approvals_for_owner(e.owner)[0]
    e.core.decide_approval(e.owner, a["id"], approve=False, note="not now")
    assert e.core._task_row(t["id"])["state"] == "cancelled"
    with pytest.raises(Conflict):
        e.core.decide_approval(e.owner, a["id"], approve=True)


def test_revise_then_accept(env):
    e = env
    t = submit(e)
    e.core.read_inbox(e.p_claude)
    e.core.respond(e.p_claude, t["id"], "v1")
    e.core.revise_task(e.owner, t["id"], "Add a risks section.")
    assert e.core._task_row(t["id"])["state"] == "in_progress"
    kinds = [i["kind"] for i in e.core.read_inbox(e.p_claude)["items"]]
    assert "revision_request" in kinds
    e.core.respond(e.p_claude, t["id"], "v2 with risks")
    assert e.core.accept_task(e.owner, t["id"])["result"] == "v2 with risks"


def test_cancel_by_creator_and_owner(env):
    e = env
    t = submit(e)
    with pytest.raises(Forbidden):
        e.core.cancel_task(e.p_claude, t["id"])  # assignee cannot cancel
    e.core.cancel_task(e.p_gpt, t["id"], "changed plans")
    assert e.core._task_row(t["id"])["state"] == "cancelled"
    notices = e.core.read_inbox(e.p_claude)["items"]
    assert any(i["kind"] == "notice" and "cancelled" in i["content"] for i in notices)
    with pytest.raises(Conflict):
        e.core.respond(e.p_claude, t["id"], "late reply")
    t2 = submit(e, title="Other", brief="Other brief")
    assert e.core.cancel_task(e.owner, t2["id"])["state"] == "cancelled"


def test_agent_requests_human_input(env):
    e = env
    t = submit(e)
    e.core.read_inbox(e.p_claude)
    r = e.core.request_approval(e.p_claude, t["id"], "May I include the budget figures?")
    assert r["task"]["state"] == "awaiting_human_input"
    e.core.decide_approval(e.owner, r["approval_id"], approve=False, note="No budget figures.")
    assert e.core._task_row(t["id"])["state"] == "in_progress"
    msgs = e.core.read_inbox(e.p_claude)["items"]
    assert any("No budget figures" in m["content"] for m in msgs)


def test_dependencies_hold_delivery_until_accepted(env):
    e = env
    first = submit(e, title="Step 1", brief="Outline.")
    second = e.core.submit_task(e.p_gpt, e.room["id"], e.claude["id"], "Step 2", "Draft from outline.",
                                info_category="project", depends_on=[first["id"]])
    assert second["state"] == "queued"
    e.core.read_inbox(e.p_claude)
    e.core.respond(e.p_claude, first["id"], "outline")
    e.core.accept_task(e.owner, first["id"])
    assert e.core._task_row(second["id"])["state"] == "delivered"


def test_deadline_expiry(env):
    e = env
    t = submit(e)
    e.db.run("UPDATE tasks SET deadline='2000-01-01T00:00:00+00:00' WHERE id=?", (t["id"],))
    assert e.core.sweep()["expired_tasks"] == 1
    assert e.core._task_row(t["id"])["state"] == "expired"
    assert e.core.sweep()["expired_tasks"] == 0  # idempotent


def test_simulator_is_labeled_and_audited_as_simulated(env):
    e = env
    sa = e.core.create_agent(e.owner, "Sim A", "simulator", "simulated")
    sb = e.core.create_agent(e.owner, "Sim B", "simulator", "simulated")
    for a in (sa, sb):
        e.core.add_member(e.owner, e.room["id"], "agent", a["id"])
        e.core.create_grant(e.owner, a["id"], e.room["id"], level=2, info_categories=["general", "project"])
    sim = Simulator(e.core)
    t = e.core.submit_task(sim.principal(sa), e.room["id"], sb["id"], "Sim task", "Is the deadline realistic?",
                           info_category="project")
    sim.step_all(e.owner)   # B answers
    sim.step_all(e.owner)   # A synthesizes
    msgs = e.db.all("SELECT * FROM messages WHERE task_id=? AND kind IN ('response','synthesis')", (t["id"],))
    assert len(msgs) == 2 and all(m["body"].startswith(BANNER) for m in msgs)
    assert "[SIMULATED]" in e.core.get_agent(sa["id"])["label"]
    rows = e.core.audit.list(500)
    assert any(r["actor_id"] == sb["id"] and r["detail"].get("simulated") is True for r in rows)


def test_simulator_start_requires_two_simulated_members(env):
    from commons.errors import CommonsError
    e = env
    sim = Simulator(e.core)
    with pytest.raises(CommonsError):
        sim.start_test_task(e.owner, e.room["id"])
    for n in ("Sim A", "Sim B"):
        a = e.core.create_agent(e.owner, n, "simulator", "simulated")
        e.core.add_member(e.owner, e.room["id"], "agent", a["id"])
    t = sim.start_test_task(e.owner, e.room["id"])
    assert t["state"] == "awaiting_authorization"   # no grants yet: owner must approve


def test_simulator_explains_missing_permission(env):
    e = env
    sim = Simulator(e.core)
    ids = []
    for n in ("Sim A", "Sim B"):
        a = e.core.create_agent(e.owner, n, "simulator", "simulated")
        e.core.add_member(e.owner, e.room["id"], "agent", a["id"])
        ids.append(a["id"])
    t = sim.start_test_task(e.owner, e.room["id"])
    e.core.decide_approval(e.owner, e.core.approvals_for_owner(e.owner)[0]["id"], True)
    report = sim.step_all(e.owner)["actions"]
    assert any("no permission to read" in a.get("error", "") for a in report)
    for aid in ids:
        e.core.create_grant(e.owner, aid, e.room["id"], level=2, info_categories=["general", "project"])
    sim.step_all(e.owner); sim.step_all(e.owner)
    assert e.core._task_row(t["id"])["state"] == "result_ready"
