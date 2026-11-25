"""A built-in program's tool ceiling follows its contract on a hub that already has the row
(ui/seeds.py, issue #7): the stored ceiling is written again when the contract changed it, without
a new revision, and a row of a newer revision is left alone."""

from typing import Any

from sqlalchemy import select

from topcoat_hub.agent_contract import ContractError
from topcoat_hub.models import UiProfile, UiTemplate
from topcoat_hub.ui import seeds
from topcoat_hub.ui.seeds import builtin_templates, contract_scope, ensure_builtin


def _with_scope(agent: dict[str, Any], **scope: Any) -> dict[str, Any]:
    return {**agent, "tool_scope": {**agent["tool_scope"], **scope}}


async def test_a_stored_ceiling_that_differs_from_the_contract_is_written_again(client) -> None:
    sm = client.app.state.sessionmaker
    async with sm() as db:
        row = await db.get(UiTemplate, "admin")
        assert row is not None and row.builtin
        # as the hub stored it before the ceiling came from the contract: the internet left open
        row.agent = _with_scope(
            row.agent, internet=None, groups=[*row.agent["tool_scope"]["groups"], "github"]
        )
        await db.commit()
    assert await ensure_builtin(sm) >= 1
    async with sm() as db:
        row = await db.get(UiTemplate, "admin")
        assert row is not None
        assert row.agent["tool_scope"] == contract_scope("admin")
        assert row.agent["frame"]  # the rest of the program is written with it


async def test_only_the_order_of_the_groups_is_no_change(client) -> None:
    sm = client.app.state.sessionmaker
    await ensure_builtin(sm)
    async with sm() as db:
        row = await db.get(UiTemplate, "dev")
        assert row is not None
        row.agent = _with_scope(row.agent, groups=list(reversed(row.agent["tool_scope"]["groups"])))
        await db.commit()
    assert await ensure_builtin(sm) == 0


async def test_a_row_of_a_newer_revision_is_left_alone(client) -> None:
    sm = client.app.state.sessionmaker
    spec = next(t for t in builtin_templates() if t["key"] == "research")
    try:
        async with sm() as db:
            row = await db.get(UiTemplate, "research")
            assert row is not None
            row.rev = spec["rev"] + 1  # written by a newer hub
            row.agent = _with_scope(row.agent, groups=["notes"])
            await db.commit()
        await ensure_builtin(sm)
        async with sm() as db:
            row = await db.get(UiTemplate, "research")
            assert row is not None and row.agent["tool_scope"]["groups"] == ["notes"]
    finally:
        async with sm() as db:
            row = await db.get(UiTemplate, "research")
            if row is not None:
                await db.delete(row)
                await db.commit()
        await ensure_builtin(sm)


async def test_unreadable_contracts_skip_the_programs_but_not_the_profiles(
    client, monkeypatch
) -> None:
    """A broken contract keeps the stored programs as they are (no program without tools), and the
    built-in profiles, which do not depend on the contracts, are still written."""
    sm = client.app.state.sessionmaker

    def broken() -> list[dict[str, Any]]:
        raise ContractError("admin.json: broken")

    async with sm() as db:
        guest = (await db.scalars(select(UiProfile).where(UiProfile.name == "guest"))).one()
        await db.delete(guest)
        admin = await db.get(UiTemplate, "admin")
        assert admin is not None
        before = admin.agent
        await db.commit()
    monkeypatch.setattr(seeds, "builtin_templates", broken)
    assert await ensure_builtin(sm) == 1  # the guest profile
    async with sm() as db:
        assert (await db.scalars(select(UiProfile).where(UiProfile.name == "guest"))).one()
        row = await db.get(UiTemplate, "admin")
        assert row is not None and row.agent == before
