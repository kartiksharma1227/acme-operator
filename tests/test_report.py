"""The evidence report renders every section from the run's own records, and never leaks a secret."""
from agent.models import Outcome, RunState
from agent.report import write_report
from agent.state import RunStore
from tests.test_verifier import CONTRACT, report

SECRET = "hunter2-very-secret"


def test_report_has_every_section_and_no_secrets(tmp_path):
    store = RunStore(tmp_path, secrets={SECRET})
    (tmp_path / "screenshots").mkdir()
    (tmp_path / "screenshots" / "001-after-click.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    state = RunState(run_id="r1", request="Do the thing", status="completed", contract=CONTRACT,
                     outcomes=[Outcome(item="doc-1", status="created", details="R-9 made",
                                       evidence=["screenshots/001-after-click.png", "/records/R-9"])],
                     verifications=[report(["fail", "pass"]).model_dump(), report(["pass", "pass"]).model_dump()],
                     step=12, llm_calls=14, tokens_in=1000, tokens_out=100, counters={"repair_rounds": 1})
    store.save(state)
    store.append("approval_decided", {"summary": "create record", "rule_id": "FIN-3", "params": {"amount": "12800"},
                                      "approved": True, "note": "ok"})
    store.append("question_answered", {"question": "Which team?", "answer": "Sales"})
    store.append("recovery", {"type": "transient_retry"})
    store.append("recovery", {"type": "reauthenticated"})
    store.append("tool_call", {"tool": "browser_fill", "args": {"label": "Password", "value": SECRET}})

    html = write_report(tmp_path).read_text()
    for section in ("What I understood", "Results", "Independent verification", "Human involvement",
                    "Reliability", "Stats", "Full event timeline"):
        assert section in html, section
    assert 'src="screenshots/001-after-click.png"' in html
    assert "FIN-3" in html and "APPROVED" in html and "Which team?" in html
    assert "2 recoveries" in html and "1 repair round" in html
    assert SECRET not in html and "***" in html
