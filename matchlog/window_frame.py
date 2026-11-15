"""Keep the custom title bar while allowing Windows to size the window."""

import ctypes as C
from ctypes import wintypes as W
from math import ceil, isfinite
import os


MIN_WINDOW_WIDTH = 1080
MIN_WINDOW_HEIGHT = 680
GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def install_frame(title):
    """Give the WinForms borderless window a native resize edge, without a caption."""
    user32 = C.WinDLL('user32', use_last_error=True)
    user32.FindWindowW.argtypes = [W.LPCWSTR, W.LPCWSTR]
    user32.FindWindowW.restype = W.HWND
    user32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    user32.GetWindowThreadProcessId.restype = W.DWORD
    user32.GetWindowLongW.argtypes = [W.HWND, C.c_int]
    user32.GetWindowLongW.restype = W.LONG
    user32.SetWindowLongW.argtypes = [W.HWND, C.c_int, W.LONG]
    user32.SetWindowLongW.restype = W.LONG
    user32.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
    user32.SetWindowPos.restype = W.BOOL

    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return None
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(hwnd, C.byref(pid))
    if pid.value != os.getpid():
        return None

    C.set_last_error(0)
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    if not style and C.get_last_error():
        return None
    if style & WS_THICKFRAME:
        return hwnd

    # FormBorderStyle.None clears the sizing flag. Restore it without adding
    # WS_CAPTION, so the web title bar and its controls remain in place.
    C.set_last_error(0)
    changed = user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_THICKFRAME)
    if not changed and C.get_last_error():
        return None
    flags = SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags):
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        return None
    return hwnd


def set_sidebar_minimum(window, viewport_height, pixel_ratio):
    """Convert the sidebar's minimum viewport height into a native window size."""
    if (type(viewport_height) not in (int, float) or not isfinite(viewport_height)
            or not 0 < viewport_height <= 50000
            or type(pixel_ratio) not in (int, float) or not isfinite(pixel_ratio)
            or not 0.25 <= pixel_ratio <= 16):
        raise ValueError('窗口尺寸不正确')
    form = window.native
    if form is None:
        return False

    from System import Action
    from System.Drawing import Size
    from System.Windows.Forms import FormWindowState, Screen

    user32 = C.WinDLL('user32', use_last_error=True)
    user32.GetDpiForWindow.argtypes = [W.HWND]
    user32.GetDpiForWindow.restype = W.UINT
    applied = False

    def update():
        nonlocal applied
        if form.IsDisposed or form.WindowState == FormWindowState.Minimized:
            return
        chrome_width = max(0, form.Width - form.ClientSize.Width)
        chrome_height = max(0, form.Height - form.ClientSize.Height)
        dpi_scale = (user32.GetDpiForWindow(form.Handle.ToInt64()) or 96) / 96
        width = ceil(MIN_WINDOW_WIDTH * dpi_scale) + chrome_width
        height = max(ceil(MIN_WINDOW_HEIGHT * dpi_scale),ceil(viewport_height * pixel_ratio)) + chrome_height
        work_area = Screen.FromControl(form).WorkingArea
        minimum = Size(min(width, work_area.Width), min(height, work_area.Height))
        if form.MinimumSize != minimum:
            form.MinimumSize = minimum
        applied = True

    if form.InvokeRequired:
        form.Invoke(Action(update))
    else:
        update()
    return applied
