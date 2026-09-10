"""FastAPI与本地Dashboard。"""

from .app import RealtimeCollector, create_app

__all__ = ["RealtimeCollector", "create_app"]
