"""Tool layer without a browser or an LLM: registry guarantees, sandboxing, handbook, plan state."""
import json

from agent.models import ToolResult
from agent.tools import Tool, ToolRegistry

REAL_PASSWORD = "acme-demo-123"


class Boom(Tool):
    name = "boom"
    description = "always raises"

    def run(self, ctx):
        raise RuntimeError("kaboom")


def test_registry_never_raises(ctx):
    reg = ToolRegistry([Boom()])
    result = reg.run(ctx, "boom", "{}")
    assert not result.ok and result.error_type == "fatal" and "kaboom" in result.message
    assert reg.run(ctx, "nope", "{}").error_type == "fatal"
    assert reg.run(ctx, "boom", "{not json").error_type == "validation"
    assert reg.run(ctx, "boom", '{"extra": 1}').error_type == "validation"


def test_missing_required_argument(ctx, registry):
    result = registry.run(ctx, "read_pdf", {})
    assert result.error_type == "validation" and "missing ['path']" in result.message


def test_schemas_are_openai_functions(registry):
    for schema in registry.schemas():
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] and fn["description"] and fn["parameters"]["type"] == "object"
    read_only = set(registry.read_only().names())
    assert {"browser_navigate", "browser_snapshot", "browser_sign_in", "browser_follow", "browser_fill",
            "read_pdf", "get_credentials", "calculate"} <= read_only
    # nothing that can change data or involve a person
    assert not read_only & {"browser_click", "write_file", "finish", "ask_human", "record_outcome", "update_plan"}


def test_to_llm_is_compact_json():
    payload = json.loads(ToolResult.fail("not_found", "nope", data={"x": 1}).to_llm())
    assert payload == {"ok": False, "data": {"x": 1}, "error_type": "not_found", "message": "nope"}


def test_files_are_sandboxed(ctx, registry):
    listing = registry.run(ctx, "list_files", {"system": "file_inbox"})
    assert listing.ok and len(listing.data["files"]) == 6
    first = listing.data["files"][0]["path"]
    pdf = registry.run(ctx, "read_pdf", {"path": first})
    assert pdf.ok and "INVOICE" in pdf.data["text"]
    # bare file names resolve inside declared folders too
    assert registry.run(ctx, "read_pdf", {"path": listing.data["files"][0]["name"]}).ok

    assert registry.run(ctx, "read_text", {"path": ".env"}).error_type == "validation"
    assert registry.run(ctx, "read_text", {"path": "company/inbox/../../.env"}).error_type == "validation"
    assert registry.run(ctx, "write_file", {"path": "company/inbox/x.txt", "content": "x"}).error_type == "validation"

    written = registry.run(ctx, "write_file", {"path": "company/outbox/test_tools.txt", "content": "hello"})
    # Regression: a bare file name must land in the writable folder, not be refused because of the inbox.
    bare = registry.run(ctx, "write_file", {"path": "test_tools_bare.txt", "content": "hi"})
    try:
        assert written.ok and registry.run(ctx, "read_text", {"path": "company/outbox/test_tools.txt"}).data["text"] == "hello"
        assert bare.ok and bare.data["path"] == "company/outbox/test_tools_bare.txt"
    finally:
        (ctx.config.root / "company/outbox/test_tools.txt").unlink(missing_ok=True)
        (ctx.config.root / "company/outbox/test_tools_bare.txt").unlink(missing_ok=True)


def test_handbook(ctx, registry):
    index = registry.run(ctx, "list_handbook", {}).data
    assert len(index) == 2 and all(e["title"] and e["triggers"] for e in index)
    sop = registry.run(ctx, "read_handbook", {"name": index[0]["title"]})
    assert sop.ok and sop.data["text"].startswith("---")
    assert registry.run(ctx, "read_handbook", {"name": "nonexistent"}).error_type == "not_found"
    policies = registry.run(ctx, "get_policies", {}).data["policies"]
    assert "FIN-3" in policies and "SEC-2" in policies


def test_credentials_never_expose_the_password(ctx, registry, events):
    creds = registry.run(ctx, "get_credentials", {"system": "acme_erp"})
    assert creds.ok and creds.data["password"] == "$secret:acme_erp.password"
    assert REAL_PASSWORD not in creds.to_llm()
    assert REAL_PASSWORD not in json.dumps(events)
    assert registry.run(ctx, "get_credentials", {"system": "file_inbox"}).error_type == "not_found"


def test_plan_and_outcomes(ctx, registry):
    registry.run(ctx, "update_plan", {"steps": [{"id": "1", "title": "a", "status": "done"},
                                                {"id": "2", "title": "b", "status": "pending"}]})
    assert [s.status for s in ctx.state.plan] == ["done", "pending"]
    assert registry.run(ctx, "update_plan", {"steps": [{"id": "1", "title": "a", "status": "bogus"}]}).error_type == "validation"

    registry.run(ctx, "record_outcome", {"item": "doc-1", "status": "created", "details": "first"})
    registry.run(ctx, "record_outcome", {"item": "doc-1", "status": "skipped", "details": "redo"})
    assert len(ctx.state.outcomes) == 1 and ctx.state.outcomes[0].status == "skipped"

    assert registry.run(ctx, "finish", {"summary": "all done"}).ok and ctx.state.finished


def test_ask_human(ctx, registry):
    assert registry.run(ctx, "ask_human", {"question": "What number?"}).data == {"answer": "42"}
    assert "request_approval" not in registry.names()  # approvals come only from the policy gate
    redirected = registry.run(ctx, "ask_human", {"question": "Do you approve creating this record?"})
    assert redirected.error_type == "validation" and "Submit the action" in redirected.message
    assert len(ctx.human.asked) == 1  # the approval question never reached a person


def test_calculate(ctx, registry):
    calc = lambda e: registry.run(ctx, "calculate", {"expression": e})
    assert calc("3975.00 - 3500.00").data["result"] == 475
    assert calc("abs($1,203.50 - 1200) <= 5").data["result"] is True
    assert calc("abs(3975 - 3500) <= 5").data["result"] is False
    assert calc("12800 > 10000 and 1 < 2").data["result"] is True
    assert calc("__import__('os').system('x')").error_type == "validation"
    assert calc("1/0").error_type == "validation"


def test_empty_named_arguments_are_ignored(ctx, registry):
    assert registry.run(ctx, "get_policies", {"": ""}).ok
