from .archive_today import ArchiveToday
from .base import ArchiveService, ServiceResult
from .local import LocalSnapshot
from .wayback import Wayback

REGISTRY = {cls.name: cls for cls in (Wayback, ArchiveToday, LocalSnapshot)}

__all__ = ["REGISTRY", "ArchiveService", "ServiceResult"]
