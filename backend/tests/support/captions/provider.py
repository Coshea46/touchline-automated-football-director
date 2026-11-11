"""Reusable synthetic test doubles and builders; no provider calls."""


from types import SimpleNamespace
from tests.support.captions.realtime import Connection


class RawConnection(Connection):
    def __init__(self, connections):
        super().__init__(connections)
        self.websocket = SimpleNamespace()
