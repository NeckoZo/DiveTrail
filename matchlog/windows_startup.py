"""Windows shell identity, window icons, single instance and optional logon startup."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import subprocess
import sys
from uuid import UUID

RUN_KEY=r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE='Aftermatch'
APP_ID='DiveTrail.Desktop'
_app_identity=APP_ID


def set_app_identity(identity=APP_ID):
    global _app_identity
    set_identity=C.WinDLL('shell32').SetCurrentProcessExplicitAppUserModelID
    set_identity.argtypes=[W.LPCWSTR]
    set_identity.restype=W.LONG
    if set_identity(identity)>=0:
        _app_identity=identity


_APP_USER_MODEL=UUID('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3')
_PROPERTY_STORE=UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99')
_RELAUNCH_COMMAND=2
_RELAUNCH_ICON=3
_RELAUNCH_NAME=4
_WINDOW_APP_ID=5


class _PropertyKey(C.Structure):
    _fields_=[('format',C.c_byte*16),('id',W.DWORD)]


class _PropVariant(C.Structure):
    # The largest member of the native value union occupies two pointers.
    _fields_=[('type',W.WORD),('reserved',W.WORD*3),
              ('string',W.LPWSTR),('padding',C.c_void_p)]


def _write_window_properties(hwnd, properties):
    get_store=C.WinDLL('shell32').SHGetPropertyStoreForWindow
    get_store.argtypes=[W.HWND,C.c_void_p,C.POINTER(C.c_void_p)]
    get_store.restype=W.LONG
    iid=(C.c_byte*16).from_buffer_copy(_PROPERTY_STORE.bytes_le)
    store=C.c_void_p()
    if get_store(hwnd,C.byref(iid),C.byref(store))<0 or not store:
        return False

    vtable=C.cast(store,C.POINTER(C.POINTER(C.c_void_p)))[0]
    release=C.WINFUNCTYPE(W.ULONG,C.c_void_p)(vtable[2])
    set_value=C.WINFUNCTYPE(W.LONG,C.c_void_p,C.POINTER(_PropertyKey),
                           C.POINTER(_PropVariant))(vtable[6])
    key=_PropertyKey((C.c_byte*16).from_buffer_copy(_APP_USER_MODEL.bytes_le))
    success=True
    try:
        for property_id, text in properties:
            key.id=property_id
            value=_PropVariant() if text is None else _PropVariant(31,string=text)  # VT_EMPTY or VT_LPWSTR
            success=set_value(store,C.byref(key),C.byref(value))>=0 and success
    finally:
        release(store)
    return success


def set_window_taskbar(hwnd, root, portable, title, icon, installed=False):
    """Give Explorer the app's launcher and artwork when it creates a pin."""
    root=Path(root).resolve()
    if portable or installed:
        launcher=_launcher(root)
    elif getattr(sys,'frozen',False):
        launcher=Path(sys.executable)
    else:
        launcher=root/'launch.bat'
    if not launcher.is_file():
        return False

    # The ID refreshes the taskbar button, so set the relaunch information first.
    return _write_window_properties(hwnd, (
        (_RELAUNCH_COMMAND,subprocess.list2cmdline([str(launcher)])),
        (_RELAUNCH_ICON,f'{Path(icon).resolve()},0'),
        (_RELAUNCH_NAME,title),
        (_WINDOW_APP_ID,_app_identity),
    ))


def clear_window_taskbar(hwnd):
    """Release the shell properties while the window handle still exists."""
    _write_window_properties(hwnd, ((key,None) for key in (
        _WINDOW_APP_ID,_RELAUNCH_COMMAND,_RELAUNCH_ICON,_RELAUNCH_NAME)))


def _launcher(root):
    launcher=root/'DiveTrail.exe'
    legacy=root/'Apex MatchLog.exe'
    return legacy if not launcher.is_file() and legacy.is_file() else launcher


def startup_command(root):
    if '--installed' in sys.argv:
        return subprocess.list2cmdline([str(_launcher(Path(root).resolve())),'--startup'])
    if getattr(sys,'frozen',False):
        return subprocess.list2cmdline([sys.executable,'--startup'])
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    return subprocess.list2cmdline([str(pythonw),str(Path(root).resolve()/'app.py'),'--startup'])


def status(root):
    if sys.platform!='win32':return {'enabled':False,'supported':False}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,RUN_KEY) as key:value,_=winreg.QueryValueEx(key,VALUE)
        return {'enabled':value==startup_command(root),'supported':True,'path_changed':value!=startup_command(root)}
    except FileNotFoundError:return {'enabled':False,'supported':True,'path_changed':False}
    except OSError:return {'enabled':False,'supported':True,'error':'无法读取本应用的登录自启状态'}


def set_enabled(root, enabled):
    if sys.platform!='win32':raise RuntimeError('开机自启仅支持 Windows 桌面版')
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,RUN_KEY,0,winreg.KEY_SET_VALUE) as key:
        if enabled:winreg.SetValueEx(key,VALUE,0,winreg.REG_SZ,startup_command(root))
        else:
            try:winreg.DeleteValue(key,VALUE)
            except FileNotFoundError:pass


def acquire_instance(name=r'Local\AftermatchDesktop', title=None):
    k=C.windll.kernel32;u=C.windll.user32
    k.CreateMutexW.argtypes=[C.c_void_p,W.BOOL,W.LPCWSTR];k.CreateMutexW.restype=W.HANDLE
    handle=k.CreateMutexW(None,False,name)
    if not handle:raise OSError('无法创建应用单实例标记')
    if k.GetLastError()==183:
        u.FindWindowW.argtypes=[W.LPCWSTR,W.LPCWSTR];u.FindWindowW.restype=W.HWND
        titles=(title,) if title else ('DiveTrail · 尾迹','Apex MatchLog','Apex / MatchLog','Apex / Aftermatch')
        hwnd=next((handle for caption in titles if (handle:=u.FindWindowW(None,caption))),None)
        if hwnd:
            u.ShowWindow.argtypes=[W.HWND,C.c_int];u.SetForegroundWindow.argtypes=[W.HWND]
            u.ShowWindow(hwnd,9);u.SetForegroundWindow(hwnd)
        release_instance(handle);return None
    return handle


def release_instance(handle):
    C.windll.kernel32.CloseHandle.argtypes=[W.HANDLE]
    C.windll.kernel32.CloseHandle(handle)


def set_window_icon(title, path):
    """Set icons on this application's own window; pywebview's icon option skips WinForms."""
    u=C.WinDLL('user32')
    u.FindWindowW.argtypes=[W.LPCWSTR,W.LPCWSTR];u.FindWindowW.restype=W.HWND
    u.LoadImageW.argtypes=[W.HINSTANCE,W.LPCWSTR,W.UINT,C.c_int,C.c_int,W.UINT];u.LoadImageW.restype=W.HANDLE
    u.SendMessageW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM];u.SendMessageW.restype=C.c_ssize_t
    hwnd=u.FindWindowW(None,title)
    handles=[]
    if hwnd:
        for kind,size in [(0,16),(1,32)]:
            icon=u.LoadImageW(None,str(path),1,size,size,0x10)
            if icon:
                u.SendMessageW(hwnd,0x80,kind,icon)
                setter=getattr(u,'SetClassLongPtrW',u.SetClassLongW)
                setter.argtypes=[W.HWND,C.c_int,C.c_ssize_t];setter.restype=C.c_ssize_t
                setter(hwnd,-14 if kind else -34,C.cast(icon,C.c_void_p).value)
                handles.append(icon)
    return handles


def release_icons(handles):
    u=C.WinDLL('user32');u.DestroyIcon.argtypes=[W.HICON]
    for handle in handles:u.DestroyIcon(handle)
