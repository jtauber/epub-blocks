from __future__ import annotations

from dataclasses import dataclass
from zipfile import BadZipFile, ZipFile, ZipInfo
from zlib import error as ZlibError

from .errors import EpubBlocksError


@dataclass(frozen=True, slots=True)
class SafetyLimits:
    """Resource limits applied while reading and parsing an EPUB."""

    max_archive_members: int = 10_000
    max_member_bytes: int = 16 * 1024 * 1024
    max_total_read_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 1_000
    max_xml_bytes: int = 8 * 1024 * 1024
    max_xml_elements: int = 200_000
    max_xml_depth: int = 256

    def __post_init__(self) -> None:
        values: tuple[tuple[str, object], ...] = (
            ("max_archive_members", self.max_archive_members),
            ("max_member_bytes", self.max_member_bytes),
            ("max_total_read_bytes", self.max_total_read_bytes),
            ("max_compression_ratio", self.max_compression_ratio),
            ("max_xml_bytes", self.max_xml_bytes),
            ("max_xml_elements", self.max_xml_elements),
            ("max_xml_depth", self.max_xml_depth),
        )
        for name, value in values:
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_SAFETY_LIMITS = SafetyLimits()


def _validate_member_name(name: str) -> None:
    components = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(component == ".." for component in components)
    ):
        raise EpubBlocksError(f"EPUB contains an unsafe archive path: {name!r}")


class EpubArchive:
    """A bounded, unambiguous reader around an open EPUB ZIP container."""

    def __init__(
        self,
        archive: ZipFile,
        limits: SafetyLimits = DEFAULT_SAFETY_LIMITS,
    ) -> None:
        self._archive = archive
        self.limits = limits
        self._bytes_read = 0
        self._read_members: set[str] = set()
        try:
            members = archive.infolist()
        except BadZipFile as error:
            raise EpubBlocksError("EPUB has a malformed ZIP directory") from error
        if len(members) > limits.max_archive_members:
            raise EpubBlocksError(
                "EPUB has too many archive members "
                f"({len(members)} > {limits.max_archive_members})"
            )
        seen: set[str] = set()
        for member in members:
            _validate_member_name(member.filename)
            if member.filename in seen:
                raise EpubBlocksError(
                    f"EPUB contains duplicate archive path {member.filename!r}"
                )
            seen.add(member.filename)
            if member.flag_bits & 0x1:
                raise EpubBlocksError(
                    f"EPUB archive member {member.filename!r} is encrypted"
                )

    def _member(self, name: str) -> ZipInfo:
        _validate_member_name(name)
        try:
            member = self._archive.getinfo(name)
        except KeyError as error:
            raise EpubBlocksError(f"EPUB archive member {name!r} not found") from error
        if member.is_dir():
            raise EpubBlocksError(f"EPUB archive member {name!r} is a directory")
        if member.file_size > self.limits.max_member_bytes:
            raise EpubBlocksError(
                f"EPUB archive member {name!r} is too large "
                f"({member.file_size} > {self.limits.max_member_bytes} bytes)"
            )
        if member.file_size and (
            member.compress_size == 0
            or member.file_size
            > member.compress_size * self.limits.max_compression_ratio
        ):
            raise EpubBlocksError(
                f"EPUB archive member {name!r} exceeds the compression-ratio limit"
            )
        if (
            name not in self._read_members
            and self._bytes_read + member.file_size > self.limits.max_total_read_bytes
        ):
            raise EpubBlocksError(
                "EPUB extraction exceeds the total uncompressed read limit"
            )
        return member

    def read(self, name: str) -> bytes:
        """Read one member after enforcing archive and resource limits."""

        member = self._member(name)
        try:
            data = self._archive.read(member)
        except (
            BadZipFile,
            UnicodeDecodeError,
            NotImplementedError,
            RuntimeError,
            ZlibError,
        ) as error:
            raise EpubBlocksError(
                f"EPUB archive member {name!r} could not be read"
            ) from error
        if len(data) != member.file_size:
            raise EpubBlocksError(
                f"EPUB archive member {name!r} has an inconsistent size"
            )
        if name not in self._read_members:
            self._bytes_read += len(data)
            self._read_members.add(name)
        return data
