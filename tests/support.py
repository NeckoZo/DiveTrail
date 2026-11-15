"""Opt in to synthetic players for tests that exercise the recorder."""

from app import Api as DesktopApi
from matchlog.storage import Store as LocalStore

DEMO = {'self_name': 'PlayerOne', 'friends': ['队友甲', 'DemoFriend']}


def configure_demo(store):
    if not store.settings()['self_name']:
        store.configure(DEMO)


class Api(DesktopApi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_demo(self._store)


class Store(LocalStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_demo(self)
