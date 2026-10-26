"""The runtime must stay task-agnostic: no company- or task-specific words inside agent/.

All task knowledge lives in company/. If this test fails, someone hardcoded a task into
the runtime, and the 'new task = new SOP, zero code' claim stops being true.
"""
import re
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
FORBIDDEN = ["invoice", "invoices", "bill", "bills", "vendor", "vendors", "purchase order", "employee",
             "employees", "onboard", "onboarding", "laptop", "acme", "globex", "priya"]
PATTERN = re.compile(r"\b(" + "|".join(re.escape(w) for w in FORBIDDEN) + r")\b", re.IGNORECASE)


def test_agent_runtime_has_no_task_specific_words():
    hits = []
    for path in AGENT_DIR.rglob("*"):
        if path.suffix in {".py", ".md", ".j2", ".html"} and path.is_file():
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if match := PATTERN.search(line):
                    hits.append(f"{path.relative_to(AGENT_DIR.parent)}:{lineno}: '{match.group(0)}'")
    assert not hits, "Task-specific words found in the runtime:\n" + "\n".join(hits)
