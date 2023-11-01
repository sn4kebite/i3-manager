import logging

import pydbus


class NotifyHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bus = pydbus.SessionBus()
        self.notifications = self.bus.get('.Notifications')

    def notify(self, summary, body, icon='', timeout=5000):
        self.notifications.Notify('i3manager', 0, icon, summary, body, [], [], timeout)

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            icon = 'dialog-error'
        elif record.levelno >= logging.WARNING:
            icon = 'dialog-warning'
        elif record.levelno >= logging.INFO:
            icon = 'dialog-information'
        else:
            icon = ''
        self.notify('i3manager', record.getMessage(), icon=icon)
