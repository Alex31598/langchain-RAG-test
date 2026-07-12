"""Import smoke tests for the rag_mcp package stubs (M0.3)."""

import importlib

import pytest

_STUB_MODULES = [
    "rag_mcp",
    "rag_mcp.server",
    "rag_mcp.ingest",
    "rag_mcp.chunking",
    "rag_mcp.embeddings",
    "rag_mcp.retrieval",
    "rag_mcp.llm",
    "rag_mcp.config",
]


@pytest.mark.parametrize("module", _STUB_MODULES)
def test_stub_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None


def test_server_importable_from_package() -> None:
    from rag_mcp import server

    assert server is not None
