"""SP2 工作區資料模型。"""

from workspace.models import WorkspaceState
from workspace.service import WorkspaceService

__all__ = ["WorkspaceService", "WorkspaceState"]
