from dataclasses import dataclass


@dataclass
class ServiceResult:
    outcome: str  # success | retry | failed | blocked
    archive_url: str | None = None
    archive_id: str | None = None
    http_status: int | None = None
    error: str | None = None
    note: str | None = None
    job_id: str | None = None
    manual_url: str | None = None
    retry_after: float | None = None  # override the exponential backoff
    cooldown: float | None = None  # pause the whole service (rate limit / CAPTCHA wall)


class ArchiveService:
    name = ""
    label = ""
    description = ""

    def __init__(self, engine):
        self.engine = engine

    def limits(self, settings: dict) -> tuple[int, float]:
        """(max concurrent submissions, minimum seconds between two starts)."""
        return (
            int(settings.get(f"{self.name}_concurrency", 1)),
            float(settings.get(f"{self.name}_interval", 10)),
        )

    def configured(self, settings: dict) -> tuple[bool, str]:
        return True, "ready"

    async def submit(self, sub) -> ServiceResult:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def browse_url(url: str) -> str | None:
        """Public page listing this service's captures of a URL."""
        return None
