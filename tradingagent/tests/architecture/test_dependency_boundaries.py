"""Exercise core independence and inspect layer imports without third-party tools."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT / "agent_platform"
CORE_LAYERS = {
    "domain": {"domain"},
    "ports": {"domain", "ports"},
    "application": {"domain", "ports", "application"},
}
PROVIDER_ROOTS = {
    "condor",
    "config_manager",
    "hummingbot",
    "hummingbot_api_client",
    "mcp",
    "mcp_servers",
    "httpx",
    "aiohttp",
    "websockets",
    "binance",
    "sqlite3",
}


def imports(source: str, package: str) -> list[str]:
    result: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            result.append(module)
            result.extend(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            dynamic = isinstance(function, ast.Name) and function.id == "__import__"
            dynamic |= isinstance(function, ast.Attribute) and function.attr == "import_module"
            if dynamic and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    result.append(value)
    return result


def violations(source: str, layer: str, package: str) -> list[str]:
    result: list[str] = []
    for module in imports(source, package):
        parts = module.split(".")
        if parts[0] in PROVIDER_ROOTS:
            result.append(module)
        elif parts[0] == "agent_platform":
            if len(parts) > 1 and parts[1] not in CORE_LAYERS[layer]:
                result.append(module)
        elif parts[0] not in sys.stdlib_module_names and parts[0] != "pydantic":
            result.append(module)
    return result


class DependencyBoundariesTest(unittest.TestCase):
    def test_core_imports_without_condor(self):
        script = """
import importlib.abc
import pkgutil
import sys

class DenyProvider(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'condor', 'hummingbot', 'hummingbot_api_client',
                                      'mcp', 'mcp_servers', 'binance'}:
            raise ImportError('core tried to load an external provider: ' + fullname)
        return None

sys.meta_path.insert(0, DenyProvider())
import agent_platform.domain
import agent_platform.application
import agent_platform.ports
for root in (agent_platform.domain, agent_platform.application, agent_platform.ports):
    for module in pkgutil.walk_packages(root.__path__, root.__name__ + '.'):
        __import__(module.name)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_every_layer_has_an_explicit_package(self):
        for layer in (*CORE_LAYERS, "adapters", "runtime", "replay", "web"):
            with self.subTest(layer=layer):
                self.assertTrue((PACKAGE / layer / "__init__.py").is_file())

    def test_kernel_dependencies_follow_layer_direction(self):
        for layer in CORE_LAYERS:
            sources = list((PACKAGE / layer).rglob("*.py"))
            self.assertTrue(sources, f"missing {layer}")
            for path in sources:
                package = ".".join(path.parent.relative_to(PROJECT).parts)
                with self.subTest(path=path.relative_to(PROJECT)):
                    self.assertEqual(
                        violations(path.read_text(encoding="utf-8"), layer, package), []
                    )

    def test_application_rejects_adapter_dependency(self):
        sources = [
            "import condor.runtime",
            "from hummingbot_api_client import Client",
            "from agent_platform.adapters.fake import account",
            "from ..adapters.fake import account",
            "from .. import adapters",
            "from agent_platform.runtime import supervisor",
            "import sqlite3",
            "import httpx",
            "import websockets",
            "__import__('binance')",
            "importlib.import_module('agent_platform.adapters.binance_direct')",
            "import an_unapproved_sdk",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(violations(source, "application", "agent_platform.application"))

    def test_ports_cannot_hide_adapter_dependencies(self):
        self.assertTrue(
            violations(
                "from agent_platform.adapters import fake",
                "ports",
                "agent_platform.ports",
            )
        )

    def test_domain_cannot_depend_on_ports(self):
        self.assertTrue(
            violations(
                "from agent_platform.ports import account",
                "domain",
                "agent_platform.domain",
            )
        )

    def test_application_can_use_domain_and_ports(self):
        source = "from ..domain import account\nfrom ..ports import market\nimport decimal"
        self.assertEqual(violations(source, "application", "agent_platform.application"), [])


if __name__ == "__main__":
    unittest.main()
