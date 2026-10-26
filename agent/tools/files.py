"""File tools, sandboxed to the folders declared in the system directory.

The operator can only touch folders the company listed, and can only write to
folders marked `access: write`. Paths that escape (../) are refused.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pypdf import PdfReader

from agent.models import ToolResult
from agent.tools.base import Tool

if TYPE_CHECKING:
    from agent.tools.base import RunContext

MAX_CHARS = 20_000


def _folders(ctx: "RunContext") -> dict[str, tuple[Path, str]]:
    return {name: ((ctx.config.root / s["path"]).resolve(), s.get("access", "read"))
            for name, s in ctx.config.folder_systems().items()}


def _resolve(ctx: "RunContext", path: str, write: bool = False) -> tuple[Path | None, ToolResult | None]:
    folders = _folders(ctx)
    writable = [n for n, (_, a) in folders.items() if a == "write"]

    def owner(resolved: Path) -> tuple[str, str] | None:
        return next(((n, a) for n, (f, a) in folders.items() if resolved == f or f in resolved.parents), None)

    raw = Path(path)
    as_given = (raw if raw.is_absolute() else ctx.config.root / raw).resolve()
    if found := owner(as_given):  # the path names a folder: that folder's rules decide
        name, access = found
        if write and access != "write":
            return None, ToolResult.fail("validation", f"'{name}' is read-only. Writable folders: {writable}.")
        if write or as_given.exists():
            return as_given, None
    elif not raw.is_absolute():
        # A bare relative name: try it inside each declared folder (only writable ones when writing).
        for name, (folder, access) in folders.items():
            candidate = (folder / raw).resolve()
            if owner(candidate) != (name, access) or (write and access != "write"):
                continue  # escaped its folder (../) or can't be written
            if write or candidate.exists():
                return candidate, None
    return None, ToolResult.fail("validation", f"'{path}' is not inside a folder you may access "
                                               f"(or does not exist). Folders: {list(folders)}. Use list_files.")


def _rel(ctx: "RunContext", path: Path) -> str:
    return str(path.relative_to(ctx.config.root))


class ListFiles(Tool):
    name = "list_files"
    description = "List the files in a folder system from the system directory (e.g. a shared inbox folder)."
    parameters = {
        "type": "object",
        "properties": {"system": {"type": "string", "description": "Folder system name"}},
        "required": ["system"],
    }

    def run(self, ctx, system: str) -> ToolResult:
        folders = _folders(ctx)
        if system not in folders:
            return ToolResult.fail("not_found", f"No folder system '{system}'. Folder systems: {list(folders)}.")
        folder, access = folders[system]
        files = [{"path": _rel(ctx, p), "name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)}
                 for p in sorted(folder.iterdir()) if p.is_file() and not p.name.startswith(".")]
        return ToolResult.success({"system": system, "access": access, "files": files})


class ReadPdf(Tool):
    name = "read_pdf"
    description = "Extract the text of a PDF file (path as returned by list_files)."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def run(self, ctx, path: str) -> ToolResult:
        resolved, failure = _resolve(ctx, path)
        if failure:
            return failure
        text = "\n".join(page.extract_text() or "" for page in PdfReader(resolved).pages)
        return ToolResult.success({"path": _rel(ctx, resolved), "text": text[:MAX_CHARS]})


class ReadText(Tool):
    name = "read_text"
    description = "Read a text file (path as returned by list_files)."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def run(self, ctx, path: str) -> ToolResult:
        resolved, failure = _resolve(ctx, path)
        if failure:
            return failure
        return ToolResult.success({"path": _rel(ctx, resolved), "text": resolved.read_text()[:MAX_CHARS]})


class WriteFile(Tool):
    name = "write_file"
    description = "Write a text file into a writable folder system (e.g. an outbox). Overwrites existing files."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path inside a writable folder"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    risk = "write"
    read_only = False

    def run(self, ctx, path: str, content: str) -> ToolResult:
        resolved, failure = _resolve(ctx, path, write=True)
        if failure:
            return failure
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        rel = _rel(ctx, resolved)
        return ToolResult.success({"path": rel, "bytes": len(content.encode())}, evidence=[rel])


FILE_TOOLS = [ListFiles, ReadPdf, ReadText, WriteFile]
