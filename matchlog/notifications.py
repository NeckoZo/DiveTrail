"""Confirmation semantics plus a native, non-activating Windows notification window."""
from dataclasses import dataclass
import queue
import threading
import time
from matchlog.recognition import nickname


@dataclass(frozen=True)
class Notice:
    title: str
    detail: str
    tone: str='info'
    seconds: float=10
    evidence: str=''
    lobby: bool=False


def debug_position(monitor_top, monitor_bottom, work_top, work_bottom, height, *, lobby=False):
    fraction = (1000 if lobby else 1220) / 1600
    target = monitor_top + round((monitor_bottom - monitor_top) * fraction)
    return min(max(work_top, target), max(work_top, work_bottom-height))


def notice_symbol(notice):
    return '✅' if notice.tone=='success' else '🟡' if notice.tone in ('warning','error') else '⚪' if notice.tone=='info' else ''


def notice_category(notice):
    return 'success' if notice.tone=='success' else 'detect' if notice.tone=='info' else 'pending'


def saved_notice(match, own_name):
    if match.get('record_source')=='last_snapshot':return Notice('最后快照已记录','','success',5)
    summary=match.get('summary') or {}
    rank=match.get('rank') or {}
    players=summary.get('players') or []
    own=next((p for p in players if nickname(p.get('name',''))==nickname(own_name)),None)
    valid_rank=rank.get('delta') is not None
    from matchlog.completeness import record_status
    status=record_status(match,own_name)
    if status['complete'] and own:
        place=summary.get('placement')
        placement=f'第 {place} 名 · ' if place is not None else ''
        rp=f' · {rank["delta"]:+d} RP' if valid_rank else ''
        return Notice('已确认记录',f'{placement}{own["kills"]}/{own["assists"]}/{own["knocks"]} · {own["damage"]:,} 伤害'+rp,'success',12)
    missing=status['missing']
    detail='缺'+'、'.join(missing[:3]) if missing else '请核对本局详情'
    return Notice('待补全',detail+' · F8 补拍', 'warning',16)


class NativeNotifications:
    """OS-owned topmost window, no game hook, DLL, WebView, or focus stealing."""
    def __init__(self, mode='status', exclude_capture=True):
        self._queue=queue.Queue()
        self._lock=threading.Lock()
        self._stop=threading.Event()
        self._thread=None
        self._notice=Notice('','')
        self._hwnd=None
        self._ready=threading.Event()
        self._mode=mode
        self._exclude_capture=exclude_capture
        self._config={f'notify_{kind}_{field}':value for kind in ['detect','success','pending']
                      for field,value in [('enabled',True),('seconds',3 if kind=='detect' else 5)]}
        self._config['debug_mode']=False
        self._suspended=False
        self._hide_pending=False

    def suspend(self, value):
        with self._lock:
            self._suspended=bool(value)
            if self._suspended:
                self._take_latest()
                self._hide_pending=True

    def configure(self, config):
        with self._lock:
            self._config={**self._config,**config}

    @property
    def debug_enabled(self):
        return bool(self._config['debug_mode'])

    def start(self):
        self._thread=threading.Thread(target=self._run,daemon=True,name='record-notification')
        self._thread.start()

    def _enabled(self, notice):
        if self._mode=='guide':return True
        if self._mode=='debug':return self._config.get('debug_mode')
        return self._config.get('notify_'+notice_category(notice)+'_enabled')

    def show(self, notice, game=None):
        with self._lock:
            if not self._suspended and self._enabled(notice) and (self._mode!='status' or notice_symbol(notice)):
                self._queue.put((notice,game))

    def _take_latest(self):
        latest=None
        while True:
            try:latest=self._queue.get_nowait()
            except queue.Empty:return latest

    def close(self):
        self._stop.set()

    def _run(self):
        import ctypes as C
        from ctypes import wintypes as W
        import logging
        # Each window thread owns its ctypes prototypes: WindowClass types are local to this call.
        u=C.WinDLL('user32',use_last_error=True)
        g=C.WinDLL('gdi32',use_last_error=True)
        k=C.WinDLL('kernel32',use_last_error=True)
        ptr=C.c_ssize_t
        callback=C.WINFUNCTYPE(ptr,W.HWND,W.UINT,W.WPARAM,W.LPARAM)
        class WindowClass(C.Structure):
            _fields_=[('style',W.UINT),('proc',callback),('class_extra',C.c_int),('window_extra',C.c_int),
                      ('instance',W.HINSTANCE),('icon',W.HICON),('cursor',W.HANDLE),('background',W.HBRUSH),
                      ('menu',W.LPCWSTR),('name',W.LPCWSTR)]
        class Paint(C.Structure):
            _fields_=[('dc',W.HDC),('erase',W.BOOL),('rect',W.RECT),('restore',W.BOOL),('update',W.BOOL),('reserved',C.c_byte*32)]
        class MonitorInfo(C.Structure):
            _fields_=[('size',W.DWORD),('monitor',W.RECT),('work',W.RECT),('flags',W.DWORD)]
        k.GetModuleHandleW.argtypes=[W.LPCWSTR];k.GetModuleHandleW.restype=W.HMODULE
        u.CreateWindowExW.argtypes=[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p]
        u.CreateWindowExW.restype=W.HWND
        u.DefWindowProcW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM];u.DefWindowProcW.restype=ptr
        u.BeginPaint.argtypes=[W.HWND,C.POINTER(Paint)];u.BeginPaint.restype=W.HDC
        u.EndPaint.argtypes=[W.HWND,C.POINTER(Paint)]
        u.SetWindowPos.argtypes=[W.HWND,W.HWND,C.c_int,C.c_int,C.c_int,C.c_int,W.UINT]
        u.ShowWindow.argtypes=[W.HWND,C.c_int]
        u.DestroyWindow.argtypes=[W.HWND]
        u.SetLayeredWindowAttributes.argtypes=[W.HWND,W.DWORD,W.BYTE,W.DWORD]
        u.SetWindowDisplayAffinity.argtypes=[W.HWND,W.DWORD]
        u.InvalidateRect.argtypes=[W.HWND,C.POINTER(W.RECT),W.BOOL]
        u.MonitorFromRect.argtypes=[C.POINTER(W.RECT),W.DWORD];u.MonitorFromRect.restype=W.HANDLE
        u.GetMonitorInfoW.argtypes=[W.HANDLE,C.POINTER(MonitorInfo)]
        u.GetDC.argtypes=[W.HWND];u.GetDC.restype=W.HDC
        u.ReleaseDC.argtypes=[W.HWND,W.HDC]
        window_dpi=getattr(u,'GetDpiForWindow',None)
        if window_dpi:
            window_dpi.argtypes=[W.HWND];window_dpi.restype=W.UINT
        g.CreateSolidBrush.argtypes=[W.DWORD];g.CreateSolidBrush.restype=W.HBRUSH
        g.SelectObject.argtypes=[W.HDC,W.HANDLE];g.SelectObject.restype=W.HANDLE
        g.DeleteObject.argtypes=[W.HANDLE]
        g.SetTextColor.argtypes=[W.HDC,W.DWORD]
        g.SetBkMode.argtypes=[W.HDC,C.c_int]
        g.GetStockObject.argtypes=[C.c_int];g.GetStockObject.restype=W.HANDLE
        g.Ellipse.argtypes=[W.HDC,C.c_int,C.c_int,C.c_int,C.c_int]
        g.CreateFontW.argtypes=[C.c_int]*5+[W.DWORD]*8+[W.LPCWSTR]
        g.CreateFontW.restype=W.HANDLE
        u.FillRect.argtypes=[W.HDC,C.POINTER(W.RECT),W.HBRUSH]
        u.DrawTextW.argtypes=[W.HDC,W.LPCWSTR,C.c_int,C.POINTER(W.RECT),W.UINT]
        u.RegisterClassW.argtypes=[C.POINTER(WindowClass)]
        u.UnregisterClassW.argtypes=[W.LPCWSTR,W.HINSTANCE]
        scale=1
        try:scale=(u.GetDpiForSystem() or 96)/96
        except AttributeError:pass
        px=lambda value:round(value*scale)
        guide=self._mode=='guide'
        debug=self._mode=='debug'
        text_mode=debug or guide
        size=(385,66) if guide else (1,68) if debug else (28,28)
        width,height=map(px,size)
        debug_scale=scale
        debug_px=lambda value:round(value*debug_scale)
        debug_text_flags=0x20|0x800  # DT_SINGLELINE | DT_NOPREFIX
        debug_padding=debug_px(6)
        debug_layout=None
        debug_monitor=None
        rgb=lambda r,gg,b:r|(gg<<8)|(b<<16)
        transparent_color=rgb(19,27,35) if text_mode else rgb(1,2,3)
        background=g.CreateSolidBrush(transparent_color)
        def create_font(font_height):
            return g.CreateFontW(-font_height,0,0,0,400,0,0,0,1,0,0,4,0,
                                 'Microsoft YaHei UI' if text_mode else 'Segoe UI Emoji')

        titlefont=create_font(px(14 if guide else 12 if debug else 20))

        def line_width(dc, lines, font):
            old=g.SelectObject(dc,font)
            try:
                widths=[]
                for text in lines:
                    bounds=W.RECT()
                    u.DrawTextW(dc,text,-1,C.byref(bounds),debug_text_flags|0x400)  # DT_CALCRECT
                    widths.append(bounds.right)
                return max(widths)
            finally:
                g.SelectObject(dc,old)
        @callback
        def wndproc(hwnd,msg,wparam,lparam):
            if msg==0x84:return -1  # HTTRANSPARENT: input continues to the game.
            if msg==0x21:return 3   # MA_NOACTIVATE.
            if msg==0xF:
                paint=Paint();dc=u.BeginPaint(hwnd,C.byref(paint))
                try:
                    rect=W.RECT(0,0,width,height);u.FillRect(dc,C.byref(rect),background)
                    color=rgb(222,230,236) if debug else rgb(159,185,137) if self._notice.tone=='success' else rgb(141,152,161) if self._notice.tone=='info' else rgb(207,180,112)
                    g.SetBkMode(dc,1)
                    old=g.SelectObject(dc,titlefont);g.SetTextColor(dc,color)
                    if guide:
                        for text,top in ((self._notice.title,10),(self._notice.detail,35)):
                            rect=W.RECT(px(14),px(top),width-px(14),height)
                            u.DrawTextW(dc,text,-1,C.byref(rect),0x20|0x8000|0x800)
                    elif debug:
                        for text,top,color in (
                            (self._notice.title,7,rgb(222,230,236)),
                            (self._notice.detail,27,rgb(187,199,211)),
                            (self._notice.evidence,47,rgb(177,208,154)),
                        ):
                            g.SetTextColor(dc,color)
                            rect=W.RECT(debug_padding,debug_px(top),width-debug_padding,height)
                            u.DrawTextW(dc,text,-1,C.byref(rect),debug_text_flags)
                    elif self._notice.tone=='success':
                        rect=W.RECT(0,0,width,height)
                        u.DrawTextW(dc,notice_symbol(self._notice),-1,C.byref(rect),0x20|0x4|0x1|0x800)
                    else:
                        # GDI renders the yellow-circle color-font fallback hatched. Draw its solid circle shape.
                        brush=g.CreateSolidBrush(color)
                        oldbrush=g.SelectObject(dc,brush);oldpen=g.SelectObject(dc,g.GetStockObject(8))
                        margin=11 if self._notice.tone=='info' else 7
                        g.Ellipse(dc,px(margin),px(margin),width-px(margin),height-px(margin))
                        g.SelectObject(dc,oldpen);g.SelectObject(dc,oldbrush);g.DeleteObject(brush)
                    g.SelectObject(dc,old)
                finally:u.EndPaint(hwnd,C.byref(paint))
                return 0
            return u.DefWindowProcW(hwnd,msg,wparam,lparam)
        instance=k.GetModuleHandleW(None)
        name=f'AftermatchNotice-{id(self)}'
        cls=WindowClass(0,wndproc,0,0,instance,None,None,background,None,name)
        try:
            if not u.RegisterClassW(C.byref(cls)):raise OSError('RegisterClass failed')
            # TOPMOST | TOOLWINDOW | NOACTIVATE | TRANSPARENT | LAYERED.
            caption='DiveTrail · 档案提示' if guide else 'DiveTrail · 记录提示'
            hwnd=u.CreateWindowExW(0x8|0x80|0x08000000|0x20|0x80000,name,caption,0x80000000,0,0,width,height,None,None,instance,None)
            if not hwnd:raise OSError('CreateWindowEx failed')
            u.SetLayeredWindowAttributes(hwnd,transparent_color,230 if guide else 155 if text_mode else 205,
                                         2 if text_mode else 1|2)
            if self._exclude_capture:
                u.SetWindowDisplayAffinity(hwnd,0x11)
            self._hwnd=hwnd;self._ready.set()
            deadline=0;message=W.MSG()
            while not self._stop.wait(.04):
                while u.PeekMessageW(C.byref(message),None,0,0,1):
                    u.TranslateMessage(C.byref(message));u.DispatchMessageW(C.byref(message))
                with self._lock:
                    now=time.monotonic()
                    if self._hide_pending or (deadline and (now>=deadline or not self._enabled(self._notice))):
                        u.ShowWindow(hwnd,0);deadline=0
                        self._hide_pending=False
                    latest=self._take_latest()
                    if self._suspended or latest is None:continue
                    incoming,game=latest
                    if not self._enabled(incoming):continue
                    # Preserve a visible result when a quiet detection dot arrives.
                    if self._mode=='status' and incoming.tone=='info' and deadline and self._notice.tone!='info':
                        continue
                    self._notice=incoming
                    if game:
                        rect=W.RECT(game['left'],game['top'],game['left']+game['width'],game['top']+game['height'])
                    else:rect=W.RECT(0,0,1,1)
                    monitor=MonitorInfo();monitor.size=C.sizeof(monitor)
                    monitor_handle=u.MonitorFromRect(C.byref(rect),2)
                    u.GetMonitorInfoW(monitor_handle,C.byref(monitor))
                    area=monitor.work
                    if debug:
                        if window_dpi and monitor_handle!=debug_monitor:
                            # Move onto the target monitor first so the overlay's DPI
                            # is used even when the game is system-DPI-aware.
                            u.SetWindowPos(hwnd,None,area.left,area.top,0,0,0x1|0x4|0x10)
                            debug_monitor=monitor_handle
                        dpi=window_dpi(hwnd) if window_dpi else 0
                        debug_scale=dpi/96 if dpi else scale
                        margin=debug_px(8)
                        available=max(1,area.right-area.left-margin)
                        lines=(incoming.title,incoming.detail,incoming.evidence)
                        layout=(lines,debug_scale,available)
                        if layout!=debug_layout:
                            dc=u.GetDC(hwnd)
                            if dc:
                                try:
                                    debug_padding=min(debug_px(6), (available-1)//2)
                                    text_space=max(1,available-2*debug_padding-1)
                                    font_height=debug_px(12)
                                    font=create_font(font_height)
                                    measured=line_width(dc,lines,font)
                                    while measured>text_space and font_height>1:
                                        g.DeleteObject(font)
                                        font_height=max(1,font_height*text_space//measured)
                                        font=create_font(font_height)
                                        measured=line_width(dc,lines,font)
                                    g.DeleteObject(titlefont)
                                    titlefont=font
                                    width=min(available,measured+2*debug_padding+1)
                                    height=debug_px(68)
                                    debug_layout=layout
                                finally:
                                    u.ReleaseDC(hwnd,dc)
                        x=max(area.left,area.right-width-margin)
                        y=debug_position(monitor.monitor.top,monitor.monitor.bottom,area.top,area.bottom,height,lobby=incoming.lobby)
                    else:x,y=area.right-width-px(20),area.bottom-height-px(18)
                    u.SetWindowPos(hwnd,W.HWND(-1),x,y,width,height,0x10|0x40)
                    u.InvalidateRect(hwnd,None,False)
                    duration=incoming.seconds if guide else 8 if debug else self._config.get('notify_'+notice_category(incoming)+'_seconds',5)
                    deadline=time.monotonic()+duration
            u.DestroyWindow(hwnd)
        except Exception:
            logging.exception('native notification unavailable')
            self._ready.set()
        finally:
            self._hwnd=None
            u.UnregisterClassW(name,instance)
            for obj in [titlefont,background]:g.DeleteObject(obj)
