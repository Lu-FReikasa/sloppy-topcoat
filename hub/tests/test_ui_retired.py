"""A built-in workspace that is no longer offered is removed on start (ui.seeds.RETIRED_BUILTINS).

the inbox is part of Chat now, so its stored template must not stay in the program list."""

from topcoat_hub.models import UiTemplate
from topcoat_hub.ui.seeds import RETIRED_BUILTINS, SYSTEM_ID, ensure_builtin


async def test_a_retired_builtin_template_is_removed_and_nothing_else_is(client) -> None:
    sm = client.app.state.sessionmaker
    assert "inbox" in RETIRED_BUILTINS
    async with sm() as db:
        db.add(
            UiTemplate(
                key="inbox",
                owner_id=SYSTEM_ID,
                builtin=True,
                title="Inbox",
                icon="@",
                position=5,
                modules=["page.inbox"],
                agent={},
                rev=1,
                open=None,
            )
        )
        await db.commit()
    await ensure_builtin(sm)
    async with sm() as db:
        assert await db.get(UiTemplate, "inbox") is None
        assert await db.get(UiTemplate, "chat") is not None
