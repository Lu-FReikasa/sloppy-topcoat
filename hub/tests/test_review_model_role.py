"""Which row of the table a model is taken from: a reviewer run asks for the check row (PPH-05).

A reviewer run is the second look at a message from another application. Its model must come
from the check row, so that its node can differ from the project chat's node. An ordinary run of
the same application keeps the agent row of that application.
"""

from types import SimpleNamespace
from typing import Any

from topcoat_hub.agent_service import GatewayModel
from topcoat_hub.job_log import set_job_context


class _RecordingResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def resolve(self, role: str, *, application: str | None = None, **_: Any) -> Any:
        self.calls.append((role, application))
        return SimpleNamespace(requested="model@node")


def _service(resolver: _RecordingResolver) -> GatewayModel:
    service = GatewayModel.__new__(GatewayModel)
    service.gateway = SimpleNamespace(resolver=resolver)  # type: ignore[attr-defined]
    return service


async def test_a_reviewer_run_asks_for_the_check_row_of_its_application() -> None:
    resolver = _RecordingResolver()
    set_job_context(role="check", application="project")
    await _service(resolver)._model(None)
    assert resolver.calls == [("check", "project")]


async def test_an_ordinary_run_of_the_same_application_keeps_the_agent_row() -> None:
    resolver = _RecordingResolver()
    set_job_context(role=None, application="project")
    await _service(resolver)._model(None)
    assert resolver.calls == [("agent", "project")]
