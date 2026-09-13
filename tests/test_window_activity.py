import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from quota_guard.web_host import DesktopHost


def test_native_activation_and_deactivation_change_presentation_state(monkeypatch):
    class Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def fire(self):
            for handler in self.handlers:
                handler(None, None)

    system, drawing = ModuleType('System'), ModuleType('System.Drawing')
    drawing.Size = lambda width, height: (width, height)
    monkeypatch.setitem(sys.modules, 'System', system)
    monkeypatch.setitem(sys.modules, 'System.Drawing', drawing)
    form = SimpleNamespace(DeviceDpi=96, Activated=Event(), Deactivate=Event())
    controller = Mock()
    host = DesktopHost(SimpleNamespace(native=form), controller, SimpleNamespace())
    host.invoke = lambda callback: callback()
    host.corners = Mock()
    host.prepare()
    form.Deactivate.fire()
    controller._set_hidden.assert_called_with(True)
    form.Activated.fire()
    controller._set_hidden.assert_called_with(False)
