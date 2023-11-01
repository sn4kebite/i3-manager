import asyncio
import logging
import signal
import sys

from i3ipc.aio import Connection
import systemd
import systemd.journal

from .args import args
from .manager import Manager
from .notify import NotifyHandler


logger = logging.getLogger('i3manager')
if sys.stdin.isatty():
    logging.getLogger().addHandler(logging.StreamHandler())
else:
    logging.getLogger().addHandler(systemd.journal.JournalHandler(SYSLOG_IDENTIFIER='i3manager'))
logging.getLogger().addHandler(NotifyHandler(level=logging.WARNING))

if args.debug:
    logger.setLevel(level=logging.DEBUG)
else:
    logger.setLevel(level=logging.INFO)


async def main():
    logger.info('Starting')
    import i3ipc.aio.connection
    logger.debug('socket path %r', await i3ipc.aio.connection._find_socket_path())
    i3 = await Connection().connect()
    manager = await Manager(i3).start()
    done, pending = await asyncio.wait([
        asyncio.create_task(i3.main()),
        asyncio.create_task(exit_event.wait()),
    ], return_when=asyncio.FIRST_COMPLETED)
    i3.main_quit()
    await manager.stop()
    await asyncio.gather(*pending)


def exit_handler():
    if exit_event.is_set():
        logger.warning('Exit event already set; forcing exit')
        loop.stop()
        return
    logger.info('Stopping')
    exit_event.set()


exit_event = asyncio.Event()

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.add_signal_handler(signal.SIGINT, exit_handler)
loop.add_signal_handler(signal.SIGTERM, exit_handler)
loop.add_signal_handler(signal.SIGQUIT, exit_handler)

loop.run_until_complete(main())
logger.info('Exiting')
