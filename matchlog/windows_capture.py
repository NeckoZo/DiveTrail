"""Capture only the foreground Apex client area; never inspect process memory."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import threading
import time

user32=C.windll.user32
kernel32=C.windll.kernel32
user32.GetForegroundWindow.restype=W.HWND
user32.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
kernel32.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD]
kernel32.OpenProcess.restype=W.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
kernel32.CloseHandle.argtypes=[W.HANDLE]
for method in ['GetClientRect','ClientToScreen']:
    getattr(user32,method).argtypes=[W.HWND,C.c_void_p]


def dpi_aware():
    try:
        if user32.SetProcessDpiAwarenessContext(C.c_void_p(-4)):
            return
    except (AttributeError,OSError):
        pass
    user32.SetProcessDPIAware()


def foreground_game():
    hwnd=user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid=W.DWORD()
    user32.GetWindowThreadProcessId(hwnd,C.byref(pid))
    process=kernel32.OpenProcess(0x1000,False,pid.value)
    if not process:
        return None
    try:
        buf=C.create_unicode_buffer(32768)
        n=W.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(process,0,buf,C.byref(n)):
            return None
        if Path(buf.value).name.lower() not in ('r5apex.exe','r5apex_dx12.exe'):
            return None
    finally:
        kernel32.CloseHandle(process)
    rect=W.RECT()
    origin=W.POINT(0,0)
    if not user32.GetClientRect(hwnd,C.byref(rect)) or not user32.ClientToScreen(hwnd,C.byref(origin)):
        return None
    if rect.right<400 or rect.bottom<300:
        return None
    return {'hwnd':hwnd,'left':origin.x,'top':origin.y,'width':rect.right,'height':rect.bottom}


def capture_frame():
    import mss
    from PIL import Image
    game=foreground_game()
    if not game:
        raise RuntimeError('等待 Apex 位于前台；仅截取游戏窗口')
    with mss.mss() as grabber:
        frame=grabber.grab({k:game[k] for k in ('left','top','width','height')})
        img=Image.frombytes('RGB',frame.size,frame.rgb)
    if foreground_game()!=game:
        raise RuntimeError('游戏窗口已切换，已跳过本次采集')
    if _is_black(img):
        raise RuntimeError('游戏截图为黑屏，请尝试无边框窗口后重试')
    return img,game


def _is_black(image):
    import numpy as np

    pixels = np.asarray(image.resize((128, 32)))
    return pixels.mean() < 3 and pixels.std() < 2


def _grab_box(grabber, game, box):
    from PIL import Image
    left,top,right,bottom=[round(value*game['width' if i%2==0 else 'height']) for i,value in enumerate(box)]
    frame=grabber.grab({'left':game['left']+left,'top':game['top']+top,'width':right-left,'height':bottom-top})
    return Image.frombytes('RGB',frame.size,frame.rgb)


def capture_scout(include_bottom=True):
    """Gameplay uses the top 15%; the lobby corner is captured on demand."""
    import mss
    game=foreground_game()
    if not game:raise RuntimeError('等待 Apex 位于前台')
    with mss.mss() as grabber:
        top=_grab_box(grabber,game,(0,0,1,.15))
        bottom=_grab_box(grabber,game,(0,.60,.25,.87)) if include_bottom else None
    if foreground_game()!=game:raise RuntimeError('游戏窗口已切换，已跳过本次采集')
    if _is_black(top) and (bottom is None or _is_black(bottom)):
        raise RuntimeError('游戏截图为黑屏，请尝试无边框窗口后重试')
    return top,bottom,game


def capture_lobby_probe(game):
    import mss
    if foreground_game()!=game:raise RuntimeError('窗口已切换')
    with mss.mss() as grabber:bottom=_grab_box(grabber,game,(0,.60,.25,.87))
    if foreground_game()!=game:raise RuntimeError('窗口已切换')
    return bottom


def capture_selection_probe(game):
    import mss
    from matchlog.screen_scout import SELECTION_BAND, SELECTION_SLOTS
    if foreground_game()!=game:raise RuntimeError('窗口已切换')
    top,bottom=SELECTION_BAND
    with mss.mss() as grabber:
        patches=[_grab_box(grabber,game,(left,top,right,bottom)) for _,left,right in SELECTION_SLOTS]
    if foreground_game()!=game:raise RuntimeError('窗口已切换')
    return patches


def capture_hud_fingerprint(top):
    """Compare the bright HUD digits and rank readout independently of the playfield."""
    from PIL import Image
    w,h=top.size
    strip=top.crop((round(w*.75),round(h*.43),w,round(h*.80))).convert('L')
    gray=strip.resize((96,12),Image.Resampling.BILINEAR)
    return bytes(max(0,value-145)//24 for value in gray.tobytes())


def capture_hud_candidate(game, top, scope, round_id, captured_at):
    """Reuse the scout image and grab only the owner's name and session footer."""
    import mss
    from matchlog.capture_sampling import HudCandidate
    from matchlog.heroes import NAME_BOXES
    if foreground_game()!=game:raise RuntimeError('游戏窗口已切换，已跳过本次采集')
    width,height=game['width'],game['height']
    box=(round(width*.75),round(height*.065),width,round(height*.125))
    name_box=NAME_BOXES[2]
    footer_box=(0,.963,.32,1)
    with mss.mss() as grabber:
        name=_grab_box(grabber,game,name_box)
        footer=_grab_box(grabber,game,footer_box)
    if foreground_game()!=game:raise RuntimeError('游戏窗口已切换，已跳过本次采集')
    parts=((box[:2],top.crop(box)),
           ((round(width*name_box[0]),round(height*name_box[1])),name),
           ((0,round(height*.963)),footer))
    owner_key=bytes(max(0,value-140)//32 for value in name.convert('L').resize((80,12)).tobytes())
    return HudCandidate(scope,round_id,captured_at,capture_hud_fingerprint(top),(width,height),parts,owner_key)


def capture_backup_regions(kind, game, top, bottom, *, roster=True):
    """Copy small ROIs onto a black reference canvas; keep existing normalized OCR coordinates."""
    import mss
    from PIL import Image
    if foreground_game()!=game:raise RuntimeError('游戏窗口已切换，已跳过本次采集')
    w,h=game['width'],game['height']
    image=Image.new('RGB',(w,h),'black')
    if kind=='lobby':
        from matchlog.recognition import LOBBY_NAME_SEARCH_BOX
        box=(round(w*.022),round(h*.608),round(w*.222),round(h*.704))
        roi=bottom.crop((box[0],box[1]-round(h*.60),box[2],box[3]-round(h*.60)))
        image.paste(roi,box[:2])
        mode_box=(round(w*.025),round(h*.705),round(w*.235),round(h*.84))
        image.paste(bottom.crop((mode_box[0],mode_box[1]-round(h*.60),mode_box[2],mode_box[3]-round(h*.60))),mode_box[:2])
        # The nickname box sits partly below the existing top scout strip.
        with mss.mss() as grabber:name=_grab_box(grabber,game,LOBBY_NAME_SEARCH_BOX)
        image.paste(name,(round(w*LOBBY_NAME_SEARCH_BOX[0]),round(h*LOBBY_NAME_SEARCH_BOX[1])))
    else:
        box=(round(w*.75),round(h*.065),round(w*.925),round(h*.115))
        image.paste(top.crop(box),box[:2])
        rank_box=(round(w*.94),round(h*.073),w,round(h*.118))
        image.paste(top.crop(rank_box),rank_box[:2])
        from matchlog.heroes import NAME_BOXES
        squad_box=(.025,.74,.225,.965) if roster else NAME_BOXES[2]
        with mss.mss() as grabber:
            squad=_grab_box(grabber,game,squad_box)
            footer=_grab_box(grabber,game,(0,.963,.32,1))
        image.paste(squad,(round(w*squad_box[0]),round(h*squad_box[1])))
        image.paste(footer,(0,round(h*.963)))
    if foreground_game()!=game:raise RuntimeError('游戏窗口已切换，已跳过本次采集')
    return image


def capture(path):
    image,game=capture_frame()
    image.save(path)
    return game


def click_page_tab(token, game, reference):
    import mss
    import numpy as np
    from PIL import Image
    from matchlog.capture_journal import queued_frame_size
    # Recheck just the target label. A queued or imported screen cannot click a
    # different live page, and only the foreground game receives the click.
    if foreground_game()!=game or reference is None:return False
    size=(game['width'],game['height'])
    if reference.size not in (size,queued_frame_size(size,'settlement')):return False
    box=(max(0,token['x']-.004),max(0,token['y']-.003),min(1,token['right']+.004),min(.15,token['bottom']+.003))
    if box[2]<=box[0] or box[3]<=box[1]:return False
    pixels=tuple(round(v*reference.size[i%2]) for i,v in enumerate(box))
    before=reference.crop(pixels).convert('RGB')
    with mss.mss() as grabber:live=_grab_box(grabber,game,box)
    if live.size!=before.size:
        if reference.size==size:return False
        live=live.resize(before.size,Image.Resampling.LANCZOS)
    if np.abs(np.asarray(live,dtype=float)-np.asarray(before,dtype=float)).mean()>20:return False
    if foreground_game()!=game:return False
    old=W.POINT()
    if not user32.GetCursorPos(C.byref(old)):return False
    x=int(game['left']+(token['x']+token['right'])/2*game['width'])
    y=int(game['top']+(token['y']+token['bottom'])/2*game['height'])
    if not user32.SetCursorPos(x,y):return False
    try:
        if foreground_game()!=game:return False
        user32.mouse_event(0x0002,0,0,0,0)
        user32.mouse_event(0x0004,0,0,0,0)
        return True
    finally:
        user32.SetCursorPos(old.x,old.y)


def hotkey_loop(callback, stop, report):
    # F8; RegisterHotKey avoids global keyboard hooks and admin privileges.
    ident=0xA93
    warned=False
    while not user32.RegisterHotKey(None,ident,0x4000,0x77):
        if not warned:
            report('F8 暂不可用，可点击补拍按钮或导入截图')
            warned=True
        if stop.wait(5):return
    try:
        report(None)
        msg=W.MSG()
        while not stop.wait(.08):
            while user32.PeekMessageW(C.byref(msg),None,0,0,1):
                if msg.message==0x312 and msg.wParam==ident:
                    callback()
    finally:
        user32.UnregisterHotKey(None,ident)
