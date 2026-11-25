from unittest.mock import AsyncMock, MagicMock

import pytest

from topcoat_hub.issue_fixes import FixService


@pytest.mark.asyncio
async def test_last_program_without_fix():
    """Test that last_program returns 'issue_fix' when no fixes exist for an issue."""
    # Arrange
    sessionmaker = MagicMock()
    tools = MagicMock()
    settings = MagicMock()
    service = FixService(sessionmaker, tools, settings)
    service.of_issue = AsyncMock(return_value=[])

    # Act
    result = await service.last_program("user123", "issue456")

    # Assert
    assert result == "issue_fix"


@pytest.mark.asyncio
async def test_last_program_with_steps_program():
    """Test that last_program returns 'issue_steps' when the latest fix has that program."""
    # Arrange
    sessionmaker = MagicMock()
    tools = MagicMock()
    settings = MagicMock()
    service = FixService(sessionmaker, tools, settings)

    # Mock a fix with a run_id that points to an AgentTask with program "issue_steps"
    mock_fix = MagicMock()
    mock_fix.run_id = "run789"
    service.of_issue = AsyncMock(return_value=[mock_fix])
    service.program_of = AsyncMock(return_value="issue_steps")

    # Act
    result = await service.last_program("user123", "issue456")

    # Assert
    assert result == "issue_steps"
