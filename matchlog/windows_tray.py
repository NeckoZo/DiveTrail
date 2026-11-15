"""Keep the WinForms recorder available from the Windows notification area."""

import logging
import threading


class WindowsTray:
    def __init__(self, form, icon_path, recording, set_recording):
        from System.Drawing import Icon
        from System.Windows.Forms import ContextMenuStrip, FormWindowState, NotifyIcon

        self._form = form
        self._minimized = FormWindowState.Minimized
        self._normal = FormWindowState.Normal
        self._recording = recording
        self._set_recording = set_recording
        self._changing = threading.Lock()
        self._icon = self._menu = self._notification = None
        try:
            self._icon = Icon(str(icon_path))
            self._menu = ContextMenuStrip()
            self._menu.Items.Add('打开尾迹').Click += self._show
            self._record_item = self._menu.Items.Add('暂停记录')
            self._record_item.Click += self._toggle
            self._menu.Items.Add('完全退出').Click += self._exit
            self._menu.Opening += self._refresh_menu

            self._notification = NotifyIcon()
            self._notification.Icon = self._icon
            self._notification.Text = 'DiveTrail · 尾迹'
            self._notification.ContextMenuStrip = self._menu
            self._notification.DoubleClick += self._show
            self._notification.Visible = True
        except Exception:
            self.close()
            raise

    def _show(self, _sender, _args):
        self._form.Show()
        if self._form.WindowState == self._minimized:
            self._form.WindowState = self._normal
        self._form.Activate()

    def _refresh_menu(self, _sender, _args):
        self._record_item.Text = '暂停记录' if self._recording() else '继续记录'
        self._record_item.Enabled = not self._changing.locked()

    def _toggle(self, _sender, _args):
        if not self._changing.acquire(blocking=False):
            return
        desired = not self._recording()

        def change():
            try:
                self._set_recording(desired)
            except Exception:
                logging.exception('Could not change recording from the notification area')
            finally:
                self._changing.release()

        threading.Thread(target=change, daemon=True, name='tray-recording').start()

    def _exit(self, _sender, _args):
        self._form.Close()

    def close(self):
        """Release native resources on the same WinForms thread that created them."""
        if self._notification is not None:
            self._notification.Visible = False
            self._notification.Dispose()
            self._notification = None
        if self._menu is not None:
            self._menu.Dispose()
            self._menu = None
        if self._icon is not None:
            self._icon.Dispose()
            self._icon = None
