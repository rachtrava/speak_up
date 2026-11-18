"""SIMULATED agents for local development and testing.

These are deterministic, rule-based stand-ins. They use NO language model and are NOT
anyone's personal assistant. Every message they write starts with a SIMULATED banner,
their agents are registered with connection_type='simulated', and the audit ledger
records simulated=true for every action they take. They never satisfy the native pilot.
"""
from __future__ import annotations

import re

from .errors import CommonsError
from .util import Principal

BANNER = "[SIMULATED AGENT OUTPUT - rule-based test stand-in, no AI model, not a real assistant]"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 3]


def analyze(brief: str) -> str:
    sents = _sentences(brief)
    words = len(brief.split())
    questions = [s for s in sents if s.endswith("?")]
    risky = sorted({w for w in re.findall(r"[a-z]+", brief.lower())
                    if w in {"deadline", "budget", "cost", "risk", "legal", "privacy", "launch", "contract", "payment"}})
    lines = [BANNER, "", f"Independent review of a {words}-word brief.", "",
             "Key points I extracted:"]
    lines += [f"- {s}" for s in sents[:4]] or ["- (none found)"]
    lines += ["", "Open questions:"] + ([f"- {q}" for q in questions[:3]] or ["- Who is the audience, and what decision should this brief drive?"])
    lines += ["", "Risk-sensitive terms to confirm with the owner: " + (", ".join(risky) if risky else "none detected")]
    return "\n".join(lines)


def synthesize(task_title: str, response: str) -> str:
    bullets = [ln for ln in response.splitlines() if ln.startswith("- ")][:5]
    return "\n".join([BANNER, "", f"Synthesis for \"{task_title}\":",
                      "Combined view from both agents:", *bullets,
                      "", "Recommendation: owner to review and accept, revise or cancel."])


class Simulator:
    def __init__(self, core):
        self.core = core

    def principal(self, agent: dict) -> Principal:
        return Principal("agent", agent["id"], agent["owner_id"], agent["display_name"], True,
                         None, "simulator", None)

    def step_all(self, owner: Principal) -> dict:
        report = []
        agents = self.core.db.all("SELECT * FROM agents WHERE owner_id=? AND is_simulated=1 AND status='active'",
                                  (owner.id,))
        for a in agents:
            report.extend(self.step(self.principal(a)))
        return {"actions": report}

    def step(self, p: Principal) -> list[dict]:
        out = []
        try:
            inbox = self.core.read_inbox(p, limit=20)
        except CommonsError as e:
            return [{"agent": p.display_name, "error": e.message}]
        for item in inbox["items"]:
            tid = item["task_id"]
            if not tid:
                continue
            try:
                t = self.core._task_row(tid)
                if item["kind"] in ("task_request", "revision_request") and t["assigned_to_agent"] == p.id:
                    brief = item["content"]
                    if item["kind"] == "revision_request":
                        brief = t["brief"] + "\nRevision request: " + item["content"]
                    self.core.respond(p, tid, analyze(brief), final=True, idempotency_key=f"sim-{item['message_id']}")
                    out.append({"agent": p.display_name, "did": "responded", "task_id": tid})
                elif item["kind"] == "response" and t["created_by_agent"] == p.id:
                    self.core.respond(p, tid, synthesize(t["title"], item["content"]), final=True,
                                      idempotency_key=f"sim-{item['message_id']}")
                    out.append({"agent": p.display_name, "did": "synthesized", "task_id": tid})
            except CommonsError as e:
                out.append({"agent": p.display_name, "task_id": tid, "error": e.message})
        return out
