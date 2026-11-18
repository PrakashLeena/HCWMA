"""Manifest of a tool written by the Tool Builder (master document 6.6, build step 13).

The manifest is a request, never a grant: the tier is fixed by the loader (T1, read-only),
the status in the file can only say `quarantined` (the real status comes from the owner-only
state file), and v1 allows only `side_effects: none` with no filesystem and no secrets.
Network hosts must be named one by one; they are reached only through the shared web fetch.
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from integrations.mcp.tool_schemas import (
    PermissionTier,
    ToolManifest,
    ToolPermissions,
    ToolStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_ENV = "HCWMA_GENERATED_DIR"  # tests and the proof can point this elsewhere
MANIFEST_FILE = "manifest.yaml"
ADAPTER_FILE = "adapter.py"
TEST_FILE = "test_adapter.py"
TOOL_FILES = (MANIFEST_FILE, ADAPTER_FILE, TEST_FILE)
MAX_FILE_BYTES = {MANIFEST_FILE: 8_000, ADAPTER_FILE: 20_000, TEST_FILE: 20_000}

MAX_TEXT = 20_000  # one text value in or out
MAX_ITEMS = 500
MAX_FIELDS = 8
GENERATED_TIER = PermissionTier.T1  # fixed here; a manifest cannot ask for more

_NAME = r"^[a-z][a-z0-9_]{2,40}$"
_FIELD = r"^[a-z][a-z0-9_]{0,30}$"
_HOST = re.compile(r"^(?=.{4,100}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")

IN_TYPES = ("string", "integer", "number", "boolean", "list[string]")
OUT_TYPES = (*IN_TYPES, "list", "object")

_Text = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
_PY_TYPES: dict[str, Any] = {
    "string": _Text,
    "integer": StrictInt,
    "number": float,
    "boolean": StrictBool,
    "list[string]": Annotated[list[_Text], Field(max_length=MAX_ITEMS)],
    "list": Annotated[list[Any], Field(max_length=MAX_ITEMS)],
    "object": dict[str, Any],
}


def generated_root() -> Path:
    override = os.environ.get(GENERATED_ENV)
    return Path(override) if override else REPO_ROOT / "integrations" / "generated"


class GeneratedPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    network: list[str] = Field(default_factory=list, max_length=5)
    filesystem: list[str] = Field(default_factory=list, max_length=0)
    secrets: list[str] = Field(default_factory=list, max_length=0)

    @field_validator("network")
    @classmethod
    def _hosts(cls, hosts: list[str]) -> list[str]:
        for host in hosts:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError(f"'{host}': an IP address is not a host name")
            if not _HOST.match(host) or host in ("localhost",) or host.endswith(".local"):
                raise ValueError(f"'{host}' is not a plain public host name (no wildcards)")
        if len(set(hosts)) != len(hosts):
            raise ValueError("duplicate host")
        return hosts


class GeneratedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME)
    version: str = Field(default="0.1.0", pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
    created_by: Literal["tool_builder"] = "tool_builder"
    status: Literal["quarantined"] = "quarantined"
    route: Literal["local_data", "custom_api"] = "local_data"
    description: str = Field(min_length=1, max_length=300)
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str]
    permissions: GeneratedPermissions = Field(default_factory=GeneratedPermissions)
    side_effects: Literal["none"] = "none"
    approval_required: bool = False
    timeout_s: float = Field(default=10.0, gt=0, le=30)

    @model_validator(mode="after")
    def _consistent(self) -> GeneratedManifest:
        for label, fields, allowed in (
            ("inputs", self.inputs, IN_TYPES),
            ("outputs", self.outputs, OUT_TYPES),
        ):
            if len(fields) > MAX_FIELDS:
                raise ValueError(f"{label}: at most {MAX_FIELDS} fields")
            for key, kind in fields.items():
                if not re.match(_FIELD, key):
                    raise ValueError(f"{label}: bad field name '{key}'")
                if kind not in allowed:
                    raise ValueError(f"{label}.{key}: type must be one of {', '.join(allowed)}")
        if not self.outputs:
            raise ValueError("outputs: declare at least one field")
        if bool(self.permissions.network) != (self.route == "custom_api"):
            raise ValueError(
                "route 'custom_api' needs named network hosts, and only it may have them"
            )
        return self

    # -- what the catalogue and the owner see ------------------------------------------------

    def tool_manifest(self, status: ToolStatus) -> ToolManifest:
        return ToolManifest(
            name=self.name,
            version=self.version,
            created_by="tool_builder",
            status=status,
            route=self.route,
            description=self.description,
            permission_tier=GENERATED_TIER,
            approval_required=self.approval_required,
            side_effects="none",
            permissions=ToolPermissions(network=list(self.permissions.network)),
            timeout_s=self.timeout_s,
        )

    def input_model(self) -> type[BaseModel]:
        fields: dict[str, Any] = {k: (_PY_TYPES[t], ...) for k, t in self.inputs.items()}
        return create_model(  # type: ignore[call-overload,no-any-return]
            f"{self.name}_input", __config__=ConfigDict(extra="forbid"), **fields
        )

    def output_model(self) -> type[BaseModel]:
        fields: dict[str, Any] = {k: (_PY_TYPES[t], ...) for k, t in self.outputs.items()}
        return create_model(  # type: ignore[call-overload,no-any-return]
            f"{self.name}_output", __config__=ConfigDict(extra="forbid"), **fields
        )

    def plain_summary(self) -> str:
        """Owner-facing summary written by code from the manifest, not by the model."""
        hosts = self.permissions.network
        inputs = ", ".join(f"{k} ({t})" for k, t in self.inputs.items()) or "nothing"
        outputs = ", ".join(f"{k} ({t})" for k, t in self.outputs.items())
        lines = [
            f"{self.name} {self.version}: {self.description}",
            f"It takes: {inputs}.",
            f"It returns: {outputs}.",
            "It cannot read or write any file, run any program or use any password or key.",
            (
                "It may contact only: " + ", ".join(hosts) + " (read-only web pages)."
                if hosts
                else "It cannot use the network."
            ),
            "It changes nothing on this PC (read-only).",
        ]
        return "\n".join(lines)


class ManifestError(ValueError):
    """The manifest cannot be used. The message is safe to show the Tool Builder."""


def parse_manifest(text: str) -> GeneratedManifest:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest.yaml is not valid YAML: {str(exc)[:200]}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest.yaml must be a mapping")
    try:
        return GeneratedManifest.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'manifest'}: {e['msg']}" for e in exc.errors()[:6]
        )
        raise ManifestError(f"manifest.yaml: {problems}") from exc
