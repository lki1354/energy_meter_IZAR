import datetime as dt
from pathlib import Path

import pytest

from custom_components.energy_meter_izar.ftp_client import RemoteFileInfo

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


class FakeRemoteClient:
    """In-memory RemoteClient double serving a dict of filename → bytes."""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        connect_error: Exception | None = None,
    ) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.mtimes: dict[str, float] = {}
        self.connect_error = connect_error
        self.connected = False
        self.downloads: list[str] = []
        self.deleted: list[str] = []

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def list_files(self) -> list[RemoteFileInfo]:
        return [
            RemoteFileInfo(name=name, size=len(data), mtime=self.mtimes.get(name, 1000.0))
            for name, data in self.files.items()
        ]

    async def download(self, name: str) -> bytes:
        self.downloads.append(name)
        return self.files[name]

    async def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.files.pop(name, None)

    async def close(self) -> None:
        self.connected = False


def cp32_hex(timestamp: dt.datetime) -> str:
    """Encode a datetime as the 4-byte CP32 hex the gateway writes."""
    year = timestamp.year - 2000
    b1 = timestamp.minute
    b2 = timestamp.hour
    b3 = timestamp.day | ((year & 0x07) << 5)
    b4 = timestamp.month | ((year >> 3) << 4)
    return f"{b1:02X}{b2:02X}{b3:02X}{b4:02X}"


def snapshot_bytes(gateway_time: dt.datetime) -> bytes:
    """A minimal valid HC2XML snapshot (gateway header only, no meter slots)."""
    return (
        f"<HC2XML><UNIT><TYPE>60M</TYPE><MBTIME>{cp32_hex(gateway_time)}</MBTIME></UNIT>"
        "<MEM></MEM></HC2XML>"
    ).encode()
