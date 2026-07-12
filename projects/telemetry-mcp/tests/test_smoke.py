"""Import smoke tests for the telemetry_mcp package stubs (M0.2)."""

import importlib

import pytest

_STUB_MODULES = [
    "telemetry_mcp",
    "telemetry_mcp.server",
    "telemetry_mcp.simulator",
    "telemetry_mcp.models",
    "telemetry_mcp.safety",
    "telemetry_mcp.scenarios",
]


@pytest.mark.parametrize("module", _STUB_MODULES)
def test_stub_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None


def test_server_importable_from_package() -> None:
    from telemetry_mcp import server

    assert server is not None
