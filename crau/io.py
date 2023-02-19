import datetime
from dataclasses import dataclass
from pathlib import Path
from tarfile import open as tar_open
from typing import BinaryIO
from zipfile import ZipFile


@dataclass
class FileInfo:
    path: Path
    created_at: datetime.datetime
    size: int
    is_dir: bool
    fobj: BinaryIO


def dir_archive_files(file_path):
    for filename in file_path.glob("**/*"):
        stat = filename.stat()
        yield FileInfo(
            path=filename,
            created_at=datetime.datetime.fromtimestamp(stat.st_ctime),
            size=stat.st_size,
            is_dir=filename.is_dir(),
            fobj=filename.open(),
        )


def tar_archive_files(archive, inner_directory=None):
    for member in archive.getmembers():
        filename = Path(member.path)
        if inner_directory is not None:
            try:
                filename = filename.relative_to(inner_directory)
            except ValueError:  # `filename` is outside `inner_directory`, skip
                continue
        yield FileInfo(
            path=filename,
            created_at=datetime.datetime.fromtimestamp(member.mtime),
            size=member.size,
            is_dir=member.isdir(),
            fobj=archive.extractfile(member),
        )


def zip_archive_files(archive, inner_directory=None):
    for fileinfo in archive.filelist:
        filename = Path(fileinfo.filename)
        if inner_directory is not None:
            try:
                filename = filename.relative_to(inner_directory)
            except ValueError:  # `filename` is outside `inner_directory`, skip
                continue
        yield FileInfo(
            path=filename,
            created_at=datetime.datetime(*fileinfo.date_time),
            size=fileinfo.file_size,
            is_dir=fileinfo.is_dir(),
            fobj=archive.open(fileinfo.filename),
        )


def archive_files(file_path, inner_directory=None):
    """Retrieve file list from directory, .tar.(gz|bz2|xz) or .zip"""
    filename_lower = file_path.name.lower()
    if file_path.is_dir():
        yield from dir_archive_files(file_path)
    elif filename_lower.endswith(".tar.gz"):
        archive = tar_open(file_path, mode="r:gz")
        yield from tar_archive_files(archive, inner_directory)
    elif filename_lower.endswith(".tar.bz2"):
        archive = tar_open(file_path, mode="r:bz2")
        yield from tar_archive_files(archive, inner_directory)
    elif filename_lower.endswith(".tar.xz"):
        archive = tar_open(file_path, mode="r:xz")
        yield from tar_archive_files(archive, inner_directory)
    elif filename_lower.endswith(".zip"):
        archive = ZipFile(file_path)
        yield from zip_archive_files(archive, inner_directory)
