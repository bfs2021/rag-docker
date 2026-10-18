"""rag_delete_collection always confirms the delete it sends (issue #302).

The API refuses DELETE /collections/{name} without ?confirm=true. The MCP image
isn't part of the verify project, so this runs with the host's Python and stubs
the MCP SDK, the shared server and the HTTP client:

    python3 scripts/tests/test_mcp_delete.py
"""
import asyncio
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[2] / "mcp"


class _Server:
    def __init__(self):
        self.tools = {}

    def tool(self, name, description, annotations):
        def register(function):
            self.tools[name] = {"function": function, "description": description,
                                "annotations": annotations}
            return function
        return register


class _Annotations:
    def __init__(self, **hints):
        self.hints = hints


def _load_tools(calls):
    """Import mcp/tools/collections.py with everything it imports stubbed."""
    server = _Server()
    modules = {
        "mcp": types.ModuleType("mcp"),
        "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.mcpserver": types.ModuleType("mcp.server.mcpserver"),
        "mcp.server.mcpserver.exceptions": types.ModuleType("mcp.server.mcpserver.exceptions"),
        "mcp.types": types.ModuleType("mcp.types"),
        "mcpapp": types.ModuleType("mcpapp"),
        "ragclient": types.ModuleType("ragclient"),
    }
    modules["mcp.server.mcpserver.exceptions"].ToolError = type("ToolError", (Exception,), {})
    modules["mcp.types"].ToolAnnotations = _Annotations
    modules["mcpapp"].server = server

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"name": "Docs", "objects_deleted": 3}
    for method in ("get", "post", "patch", "delete"):
        setattr(modules["ragclient"], method,
                lambda path, _m=method.upper(), **kw: request(_m, path, **kw))
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    sys.path.insert(0, str(MCP_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "rag_mcp_tools_collections", MCP_DIR / "tools" / "collections.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(MCP_DIR))
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return server, module


class DeleteToolTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.server, self.module = _load_tools(self.calls)
        self.tool = self.server.tools["rag_delete_collection"]

    def test_the_delete_is_sent_with_confirm_true(self):
        result = asyncio.run(self.tool["function"]("Docs"))
        self.assertEqual(self.calls, [("DELETE", "/collections/Docs", {"params": {"confirm": "true"}})])
        self.assertEqual(result, {"name": "Docs", "objects_deleted": 3})

    def test_the_tool_still_takes_only_a_name(self):
        self.assertEqual(list(inspect.signature(self.tool["function"]).parameters), ["name"])
        self.assertTrue(self.tool["annotations"].hints["destructive_hint"])
        self.assertIn("cannot be undone", self.tool["description"])


if __name__ == "__main__":
    unittest.main()
