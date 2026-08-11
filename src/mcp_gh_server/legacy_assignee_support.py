"""Semantic normalization for frozen 0.6.x assignee selectors."""

from __future__ import annotations

from .gh_client import GhClient


async def resolve_assignee_groups(
    client: GhClient,
    *groups: list[str] | None,
) -> tuple[set[str], ...]:
    """Resolve supported symbolic assignees for authoritative readback comparison.

    The GitHub CLI accepts ``@me`` as an input selector, while structured readback
    returns the authenticated account's concrete login. Preserve the original CLI
    arguments, but normalize comparison sets so a successful self-assignment is not
    reported as a semantic mismatch.
    """

    self_login: str | None = None
    if any(group and "@me" in group for group in groups):
        account = await client.run("api", "user")
        login = account.get("login") if isinstance(account, dict) else None
        if not isinstance(login, str) or not login:
            raise RuntimeError("Unable to resolve @me to the authenticated GitHub login")
        self_login = login

    resolved: list[set[str]] = []
    for group in groups:
        names: set[str] = set()
        for value in group or []:
            names.add(self_login if value == "@me" and self_login is not None else value)
        resolved.append(names)
    return tuple(resolved)
