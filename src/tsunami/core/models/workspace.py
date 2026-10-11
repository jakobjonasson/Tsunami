"""workspace.yml model (design.md, Workspace layout).

workspace.yml holds nothing that affects MCP permissions; those live in the policy
file outside the workspace.
"""

from __future__ import annotations

from pydantic import Field

from tsunami.core.models.common import Platform, StrictModel

# The newest workspace schema this build understands. A newer workspace is refused;
# an older one is offered a migration.
SCHEMA_VERSION = 1


class WorkspaceDefaults(StrictModel):
    platform: Platform = Field(Platform.IOS_XE, description="Default platform for new devices.")
    batch_size: int = Field(
        5, ge=1, le=50, description="Batch size for push and baseline plays that set none."
    )


class WorkspaceFile(StrictModel):
    name: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    defaults: WorkspaceDefaults = WorkspaceDefaults()
