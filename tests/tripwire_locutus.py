"""Tripwire Custom Plugin for Locutus Multi-Agent Coordination & Mesh Bus.

Provides:
1. LocutusPlugin: Strict verification for Locutus CLI commands, exit codes, and JSON schemas.
2. WireProtocolValidator: Cryptographic envelope and HMAC verification per skills/locutus/references/wire_spec.md.
3. ClusterAffinityChecker: Enforces Redis Cluster {...} hash-tag slot affinity on multi-key commands.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Sequence

from tripwire._base_plugin import BasePlugin
from tripwire._timeline import Interaction


class LocutusSchemaError(Exception):
    """Raised when a Locutus CLI JSON output or wire envelope violates its specification."""


class ClusterSlotAffinityError(Exception):
    """Raised when multi-key Redis operations fail to share an identical {...} hash tag."""


@dataclass
class LocutusCommandMock:
    subcommand: str
    args: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    json_data: Any = None
    required: bool = True


class LocutusPlugin(BasePlugin):
    """Tripwire plugin specialized for Locutus multi-agent coordination layer."""

    plugin_name: ClassVar[str] = "locutus"

    def __init__(self, verifier: Any) -> None:
        super().__init__(verifier)
        self._command_mocks: list[LocutusCommandMock] = []

    def mock_command(
        self,
        subcommand: str,
        args: list[str] | None = None,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        json_data: Any = None,
        required: bool = True,
    ) -> None:
        """Register an expected Locutus CLI invocation."""
        if json_data is not None and not stdout:
            stdout = json.dumps(json_data)
        self._command_mocks.append(
            LocutusCommandMock(
                subcommand=subcommand,
                args=args or [],
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                json_data=json_data,
                required=required,
            )
        )

    def record_locutus_call(
        self,
        subcommand: str,
        args: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> Interaction:
        """Record a Locutus CLI call on the tripwire timeline."""
        details: dict[str, Any] = {
            "subcommand": subcommand,
            "args": list(args),
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        interaction = Interaction(
            source_id=f"locutus:{subcommand}",
            sequence=0,
            details=details,
            plugin=self,
        )
        self.record(interaction)
        return interaction

    def assert_locutus_call(
        self,
        subcommand: str,
        *,
        returncode: int = 0,
        args_contain: list[str] | None = None,
        stdout_contains: str | None = None,
    ) -> None:
        """Assert the next Locutus interaction on the timeline."""
        expected: dict[str, Any] = {
            "subcommand": subcommand,
            "returncode": returncode,
        }
        # Find matching interaction
        for interaction in self.verifier._timeline.all_unasserted():
            if interaction.source_id == f"locutus:{subcommand}":
                d = interaction.details
                if d.get("returncode") == returncode:
                    if args_contain:
                        args = d.get("args", [])
                        if not all(a in args for a in args_contain):
                            continue
                    if stdout_contains:
                        if stdout_contains not in d.get("stdout", ""):
                            continue
                    interaction.mark_asserted()
                    return
        raise AssertionError(
            f"No matching unasserted Locutus interaction found for subcommand={subcommand}, returncode={returncode}"
        )

    @staticmethod
    def validate_wire_envelope(payload: str | dict[str, Any], secret: str | None = None) -> dict[str, Any]:
        """Validate an inter-agent message against the Locutus Wire Specification.
        
        Requires: id, from, to, type, body, ts.
        If secret is provided and sig is present, validates cryptographic HMAC-SHA256 signature.
        """
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise LocutusSchemaError(f"Payload is not valid JSON: {exc}") from exc
        else:
            data = payload

        if not isinstance(data, dict):
            raise LocutusSchemaError(f"Wire envelope must be a JSON object, got {type(data).__name__}")

        required_fields = ["id", "from", "to", "type", "body", "ts"]
        for f in required_fields:
            if f not in data:
                raise LocutusSchemaError(f"Wire envelope missing required field '{f}': {data}")

        if not isinstance(data["id"], str) or len(data["id"]) == 0:
            raise LocutusSchemaError("Wire envelope 'id' must be a non-empty string")

        if not isinstance(data["from"], str) or len(data["from"]) == 0:
            raise LocutusSchemaError("Wire envelope 'from' must be a non-empty string")

        if not isinstance(data["body"], str):
            raise LocutusSchemaError("Wire envelope 'body' must be a string")

        # Cryptographic authentication verification
        if secret is not None and "sig" in data:
            sig = data["sig"]
            # Canonical message signature: id|from|to|body|ts
            canon = f"{data['id']}|{data['from']}|{data['to']}|{data['body']}|{data['ts']}"
            expected_sig = hmac.new(secret.encode("utf-8"), canon.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                raise LocutusSchemaError(
                    f"Cryptographic HMAC signature mismatch on wire envelope! Expected {expected_sig}, got {sig}"
                )

        return data

    @staticmethod
    def validate_json_schema(subcommand: str, json_text: str) -> Any:
        """Validate JSON output schema for a specific Locutus CLI subcommand."""
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise LocutusSchemaError(f"Locutus {subcommand} output is not valid JSON: {exc}") from exc

        if subcommand == "who":
            if not isinstance(data, list):
                raise LocutusSchemaError(f"'who --json' must return a list of agents, got {type(data).__name__}")
            for item in data:
                for req in ["name", "alive", "tags", "state", "activity", "last_seen"]:
                    if req not in item:
                        raise LocutusSchemaError(f"Agent object missing field '{req}': {item}")
        elif subcommand == "sweep":
            if not isinstance(data, dict):
                raise LocutusSchemaError(f"'sweep' must return an object, got {type(data).__name__}")
            for req in ["pruned_listeners", "pruned_agents", "foreign_listeners"]:
                if req not in data:
                    raise LocutusSchemaError(f"Sweep output missing field '{req}': {data}")
        elif subcommand == "config":
            if not isinstance(data, dict):
                raise LocutusSchemaError(f"'config show --json' must return a dict, got {type(data).__name__}")
            for req in ["redis_url", "project", "prefix", "encrypt", "heartbeat_ttl", "message_ttl"]:
                if req not in data:
                    raise LocutusSchemaError(f"Config output missing field '{req}': {data}")
        elif subcommand == "blackboard":
            if not isinstance(data, dict):
                raise LocutusSchemaError(f"'blackboard snapshot' must return a dict, got {type(data).__name__}")
            for req in ["room", "revision", "entries"]:
                if req not in data:
                    raise LocutusSchemaError(f"Blackboard snapshot missing field '{req}': {data}")
        elif subcommand == "workflow":
            if not isinstance(data, dict):
                raise LocutusSchemaError(f"'workflow status' must return a dict, got {type(data).__name__}")
            for req in ["id", "state", "steps"]:
                if req not in data:
                    raise LocutusSchemaError(f"Workflow status missing field '{req}': {data}")

        return data

    @staticmethod
    def check_cluster_affinity(keys: Sequence[str]) -> str:
        """Verify that all keys share an identical {...} hash tag for Redis Cluster slot affinity.
        
        Returns the common hash tag. Raises ClusterSlotAffinityError if tags differ or are missing.
        """
        if not keys:
            raise ClusterSlotAffinityError("Key list is empty")

        tag_pattern = re.compile(r"\{([^}]+)\}")
        tags: list[str] = []

        for key in keys:
            m = tag_pattern.search(key)
            if not m:
                raise ClusterSlotAffinityError(
                    f"Key '{key}' is missing a Redis Cluster hash tag '{{...}}'. Multi-key commands will cause CROSSSLOT errors."
                )
            tags.append(m.group(1))

        first_tag = tags[0]
        for t in tags[1:]:
            if t != first_tag:
                raise ClusterSlotAffinityError(
                    f"Hash tag mismatch between keys: found '{{{first_tag}}}' and '{{{t}}}'. Keys: {keys}"
                )

        return first_tag
