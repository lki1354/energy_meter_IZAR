"""Async FTP / FTPS / SFTP client abstraction for fetching gateway snapshot files.

Pure Python, no Home Assistant imports — the coordinator consumes the
:class:`RemoteClient` protocol and tests substitute a fake implementation.
"""

from __future__ import annotations

import contextlib
import ssl
from dataclasses import dataclass
from typing import Protocol


class FetchError(Exception):
    """Connecting to or transferring from the remote server failed."""


class FetchAuthError(FetchError):
    """The server rejected the credentials."""


@dataclass(frozen=True)
class RemoteFileInfo:
    """Directory-listing metadata of one remote file."""

    name: str
    size: int | None = None
    mtime: float | None = None


@dataclass(frozen=True)
class ConnectionConfig:
    """Everything needed to open a session to the gateway's file server."""

    protocol: str  # "ftp" | "ftps" | "sftp"
    host: str
    port: int
    username: str
    password: str
    directory: str = "/"


class RemoteClient(Protocol):
    """Minimal file-fetch interface the coordinator polls against."""

    async def connect(self) -> None: ...

    async def list_files(self) -> list[RemoteFileInfo]: ...

    async def download(self, name: str) -> bytes: ...

    async def delete(self, name: str) -> None: ...

    async def close(self) -> None: ...


def create_client(config: ConnectionConfig) -> RemoteClient:
    """Build the client matching ``config.protocol``."""
    if config.protocol == "sftp":
        return SftpClient(config)
    if config.protocol in ("ftp", "ftps"):
        return FtpClient(config)
    raise ValueError(f"unsupported protocol {config.protocol!r}")


class FtpClient:
    """FTP / implicit-FTPS client backed by ``aioftp``."""

    def __init__(self, config: ConnectionConfig) -> None:
        self._config = config
        self._client = None

    async def connect(self) -> None:
        import aioftp

        tls = ssl.create_default_context() if self._config.protocol == "ftps" else None
        client = aioftp.Client(ssl=tls)
        try:
            await client.connect(self._config.host, self._config.port)
            await client.login(self._config.username, self._config.password)
            await client.change_directory(self._config.directory)
        except aioftp.StatusCodeError as err:
            if any(code.matches("530") for code in err.received_codes):
                raise FetchAuthError(f"login rejected: {err}") from err
            raise FetchError(f"FTP error: {err}") from err
        except (OSError, ssl.SSLError, ConnectionError) as err:
            raise FetchError(f"cannot connect to {self._config.host}: {err}") from err
        self._client = client

    async def list_files(self) -> list[RemoteFileInfo]:
        import aioftp

        assert self._client is not None
        files: list[RemoteFileInfo] = []
        try:
            async for path, info in self._client.list():
                if info.get("type") != "file":
                    continue
                size = info.get("size")
                modify = info.get("modify")
                files.append(
                    RemoteFileInfo(
                        name=path.name,
                        size=int(size) if size is not None else None,
                        mtime=_mlsd_time_to_epoch(modify),
                    )
                )
        except (aioftp.StatusCodeError, OSError, ConnectionError) as err:
            raise FetchError(f"listing failed: {err}") from err
        return files

    async def download(self, name: str) -> bytes:
        import aioftp

        assert self._client is not None
        chunks: list[bytes] = []
        try:
            async with self._client.download_stream(name) as stream:
                async for block in stream.iter_by_block():
                    chunks.append(block)
        except (aioftp.StatusCodeError, OSError, ConnectionError) as err:
            raise FetchError(f"download of {name!r} failed: {err}") from err
        return b"".join(chunks)

    async def delete(self, name: str) -> None:
        import aioftp

        assert self._client is not None
        try:
            await self._client.remove_file(name)
        except (aioftp.StatusCodeError, OSError, ConnectionError) as err:
            raise FetchError(f"delete of {name!r} failed: {err}") from err

    async def close(self) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):  # best-effort teardown
            await self._client.quit()
        self._client = None


class SftpClient:
    """SFTP client backed by ``asyncssh``."""

    def __init__(self, config: ConnectionConfig) -> None:
        self._config = config
        self._conn = None
        self._sftp = None

    async def connect(self) -> None:
        import asyncssh

        try:
            self._conn = await asyncssh.connect(
                self._config.host,
                port=self._config.port,
                username=self._config.username,
                password=self._config.password,
                known_hosts=None,
            )
            self._sftp = await self._conn.start_sftp_client()
            await self._sftp.chdir(self._config.directory)
        except asyncssh.PermissionDenied as err:
            await self.close()
            raise FetchAuthError(f"login rejected: {err}") from err
        except (asyncssh.Error, OSError, ConnectionError) as err:
            await self.close()
            raise FetchError(f"cannot connect to {self._config.host}: {err}") from err

    async def list_files(self) -> list[RemoteFileInfo]:
        import asyncssh

        assert self._sftp is not None
        files: list[RemoteFileInfo] = []
        try:
            for entry in await self._sftp.readdir("."):
                filename = entry.filename
                if filename in (".", "..") or entry.attrs.type == 2:  # 2 = directory
                    continue
                files.append(
                    RemoteFileInfo(
                        name=filename,
                        size=entry.attrs.size,
                        mtime=float(entry.attrs.mtime) if entry.attrs.mtime else None,
                    )
                )
        except (asyncssh.Error, OSError, ConnectionError) as err:
            raise FetchError(f"listing failed: {err}") from err
        return files

    async def download(self, name: str) -> bytes:
        import asyncssh

        assert self._sftp is not None
        try:
            async with self._sftp.open(name, "rb") as handle:
                return await handle.read()
        except (asyncssh.Error, OSError, ConnectionError) as err:
            raise FetchError(f"download of {name!r} failed: {err}") from err

    async def delete(self, name: str) -> None:
        import asyncssh

        assert self._sftp is not None
        try:
            await self._sftp.remove(name)
        except (asyncssh.Error, OSError, ConnectionError) as err:
            raise FetchError(f"delete of {name!r} failed: {err}") from err

    async def close(self) -> None:
        if self._sftp is not None:
            self._sftp.exit()
            self._sftp = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _mlsd_time_to_epoch(modify: str | None) -> float | None:
    """Convert an MLSD ``modify`` fact (``YYYYMMDDHHMMSS``, UTC) to a UNIX epoch."""
    if not modify:
        return None
    import datetime as dt

    try:
        parsed = dt.datetime.strptime(modify[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC).timestamp()
