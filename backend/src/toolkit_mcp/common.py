"""Explicit MCP operation annotations (hints, never authorization)."""

from mcp.types import ToolAnnotations


def annotations(
    *, read_only=False, destructive=False, idempotent=False, open_world=False
):
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )
