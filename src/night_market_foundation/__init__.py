"""技能赛训协作基础服务的服务端基础包。"""

from .safety import SafetyService
from .service import DomainService

__all__ = ["DomainService", "SafetyService"]
