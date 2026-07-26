"""「輔」共用的活動三態。"""

from enum import Enum


class ActivityStatus(str, Enum):
    """所有活動只使用玩家已確認的三種主要狀態。"""

    STANDBY = "待命中"
    RUNNING = "執行中"
    COMPLETED = "已完成"
