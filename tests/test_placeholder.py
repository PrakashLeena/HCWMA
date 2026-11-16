"""Step 2 placeholder: proves the test runner and package layout work."""

import importlib

PACKAGES = [
    "agents.router",
    "agents.runtime",
    "agents.registry",
    "backend.api",
    "integrations.adapters.pc",
    "memory.pc_index",
    "model.serving",
    "security.audit",
    "voice.stt",
]


def test_packages_import() -> None:
    for name in PACKAGES:
        assert importlib.import_module(name) is not None
