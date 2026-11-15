"""Locate the installed edition's data in the current user's Documents folder."""

import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import stat
from uuid import UUID


DOCUMENTS_ID = UUID('fdd39ad0-238f-46af-adb4-6c85480369c7')
KF_FLAG_DONT_VERIFY = 0x4000
MIGRATION_MARKER = '.divetrail-media-rebase'


def _documents_path():
    folder_id = (C.c_byte * 16).from_buffer_copy(DOCUMENTS_ID.bytes_le)
    get_folder = C.WinDLL('shell32').SHGetKnownFolderPath
    get_folder.argtypes = [C.c_void_p, W.DWORD, W.HANDLE, C.POINTER(C.c_void_p)]
    get_folder.restype = W.LONG
    free_folder = C.WinDLL('ole32').CoTaskMemFree
    free_folder.argtypes = [C.c_void_p]
    free_folder.restype = None
    folder = C.c_void_p()
    try:
        if get_folder(C.byref(folder_id), KF_FLAG_DONT_VERIFY, None, C.byref(folder)) < 0 or not folder:
            raise OSError('无法确定 Windows 的「文档」文件夹位置')
        return Path(C.wstring_at(folder))
    finally:
        free_folder(folder)


def _is_reparse(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0)
                                     & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def installed_data_path(migrate=True):
    documents = _documents_path()
    target = documents / 'DiveTrail'
    legacy = documents / 'Apex MatchLog'
    if not migrate:
        return legacy if not target.exists() and legacy.is_dir() else target
    if not target.exists() and not target.is_symlink() and legacy.is_dir():
        if _is_reparse(legacy):
            return legacy
        database = legacy / 'matches.sqlite3'
        if database.is_file() and _is_reparse(database):
            return legacy
        marker = legacy / MIGRATION_MARKER
        try:
            if not marker.exists() and not marker.is_symlink():
                marker.touch(exist_ok=False)
            if not marker.is_file() or _is_reparse(marker):
                return legacy
            # On Windows rename refuses to overwrite a concurrently created destination.
            legacy.rename(target)
        except OSError:
            if legacy.is_dir() and not target.exists():
                return legacy
            raise
    marker = target / MIGRATION_MARKER
    if marker.is_file():
        if _is_reparse(target) or _is_reparse(marker):
            raise OSError('数据迁移标记位于文件系统链接中，无法继续迁移。')
        database = target / 'matches.sqlite3'
        if database.is_file():
            if _is_reparse(database):
                raise OSError('旧版战绩数据库使用文件系统链接，无法继续迁移。')
            from matchlog.data_relocation import rebase_database
            rebase_database(target, legacy)
        marker.unlink()
    return target
