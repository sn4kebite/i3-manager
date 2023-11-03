import asyncio
import collections
import functools
import itertools
import logging

from .args import args
from .config import config

from i3ipc import Event
import pydbus

logger = logging.getLogger(__name__)


class ManagerProtocol(asyncio.Protocol):
    def __init__(self, manager):
        # super().__init__()
        self.manager = manager

    def connection_made(self, transport):
        self.buffer = b''

    def data_received(self, data):
        self.buffer += data
        if self.buffer.endswith(b'\n'):
            self.process_input()

    def eof_received(self):
        pass

    @classmethod
    def factory(cls, manager):
        return functools.partial(cls, manager=manager)

    def process_input(self):
        buffer = self.buffer.decode()
        self.buffer = b''
        for command in buffer.splitlines():
            command, *args = command.split()
            if command == 'last-window':
                asyncio.create_task(self.manager.last_window())
            elif command == 'workspace-left':
                asyncio.create_task(self.manager.workspace_left())
            elif command == 'workspace-right':
                asyncio.create_task(self.manager.workspace_right())
            elif command == 'output-workspace':
                asyncio.create_task(self.manager.output_workspace(args))
            elif command == 'workspace-tree':
                asyncio.create_task(self.manager.workspace_tree())
            elif command == 'new-output-workspace':
                asyncio.create_task(self.manager.new_output_workspace())
            else:
                logger.warning('Unknown command %r', command)


class Output:
    def __init__(self, name):
        logger.debug('Creating output %s', name)
        self.name = name
        self.workspaces = []

    def __repr__(self):
        return f'<Output({self.name})>'

    def _sort_workspaces(self):
        self.workspaces.sort(key=lambda ws: ws.num)

    def add_workspace(self, workspace):
        self.workspaces.append(workspace)
        self._sort_workspaces()

    def remove_workspace(self, workspace):
        self.workspaces.remove(workspace)


class Workspace:
    def __init__(self, num, name):
        logger.debug('Creating workspace %s', name)
        self.num = num
        self.name = name
        self.focus_order = collections.deque(maxlen=50)

    def __hash__(self):
        return hash((self.num, self.name))

    def __eq__(self, other):
        return self.num == other.num and self.name == other.name

    def __repr__(self):
        return f'<Workspace({self.num}, {self.name!r})>'

    def clear(self):
        self.focus_order.clear()

    def on_window_focus(self, con):
        try:
            self.focus_order.remove(con.id)
        except ValueError:
            pass
        self.focus_order.append(con.id)

    def on_window_close(self, con):
        try:
            self.focus_order.remove(con.id)
        except ValueError:
            pass

    def has_container(self, con):
        return self.focus_order.count(con.id) > 0


class Manager:
    def __init__(self, i3):
        self.i3 = i3
        i3.on(Event.WORKSPACE_INIT, self.on_workspace_init)
        i3.on(Event.WORKSPACE_FOCUS, self.on_workspace_focus)
        i3.on(Event.WORKSPACE_EMPTY, self.on_workspace_empty)
        i3.on(Event.WORKSPACE_RENAME, self.on_workspace_rename)
        # i3.on(Event.WINDOW_FULLSCREEN_MODE, self.on_window_fullscreen)
        i3.on(Event.WINDOW_FOCUS, self.on_window_focus)
        i3.on(Event.WINDOW_CLOSE, self.on_window_close)
        i3.on(Event.WINDOW_MOVE, self.on_window_move)
        self.bus = pydbus.SessionBus()
        self.notifications = self.bus.get('.Notifications')
        self.server = None
        self.workspaces = {}
        self.outputs = {}
        self.current_output = None
        self.current_workspace = None
        self.scratchpad = None
        self._last_tree_notification = 0

    async def start(self):
        logger.info('Starting manager server at %s', args.socket)
        self.server = await asyncio.get_running_loop().create_unix_server(ManagerProtocol.factory(self), args.socket)
        await self._init()
        return self

    async def stop(self):
        pass

    async def _init(self):
        class State:
            current_workspace = None
            current_output = None

        def _walk_tree(node):
            if node.focused:
                logger.debug('focused node %r %s %s', node, node.type, node.name)
            if node.type == 'output' and not node.name.startswith('__i3'):
                State.current_output = self.get_output(node)
            elif node.type == 'workspace' and not node.name.startswith('__i3'):
                State.current_workspace = self.get_workspace(node)
                State.current_output.add_workspace(State.current_workspace)
                start = self.get_output_start(State.current_output)
                if not (start <= State.current_workspace.num < start + 100):
                    rename_workspaces.append((State.current_output, State.current_workspace))
                if node.focused:
                    self.current_output = State.current_output
                    self.current_workspace = State.current_workspace
                    logger.debug('Found current workspace: %s', self.current_workspace.name)
            elif node.type == 'workspace' and node.name == '__i3_scratch':
                State.current_workspace = self.scratchpad = self.get_workspace(node)
            # elif node.type in ('con', 'floating_con') and node.window and State.current_workspace:
            elif node.type == 'con' and (node.window or node.pid) and State.current_workspace:
                if node.focused:
                    self.current_output = State.current_output
                    self.current_workspace = State.current_workspace
                    logger.debug('Found current workspace %s from con %s', self.current_workspace.name, node.id)
                State.current_workspace.on_window_focus(node)

            for subnode in node.nodes:
                _walk_tree(subnode)
            for subnode in node.floating_nodes:
                _walk_tree(subnode)

        rename_workspaces = []
        _walk_tree(await self.i3.get_tree())

        for output, workspace in rename_workspaces:
            start = self.get_output_start(output)
            preferred = start + (workspace.num % 100) - 1
            nums = set(ws.num for ws in output.workspaces)
            for i in itertools.chain((preferred,), itertools.count(start)):
                if i in nums:
                    continue
                if workspace.name != str(workspace.num):
                    from_name = f'"{workspace.num}:{workspace.name}"'
                    to_name = f'"{i}:{workspace.name}"'
                else:
                    from_name = workspace.num
                    to_name = i
                await self.i3.command(f'rename workspace {from_name} to {to_name}')
                break

    # Commands

    async def last_window(self):
        if not self.current_workspace:
            logger.warning('Cannot focus last window; no current workspace found')
            return
        if len(self.current_workspace.focus_order) < 2:
            logger.warning('Cannot focus last window; no history')
            return
        last_window = self.current_workspace.focus_order[-2]
        await self.i3.command('[con_id={}] focus'.format(last_window))

    async def workspace_left(self):
        index = self.current_output.workspaces.index(self.current_workspace)
        if index == 0:
            index = len(self.current_output.workspaces) - 1
        else:
            index -= 1
        workspace = self.current_output.workspaces[index]
        await self.i3.command(f'workspace {workspace.name}')

    async def workspace_right(self):
        index = self.current_output.workspaces.index(self.current_workspace)
        if index >= len(self.current_output.workspaces) - 1:
            index = 0
        else:
            index += 1
        workspace = self.current_output.workspaces[index]
        await self.i3.command(f'workspace {workspace.name}')

    async def output_workspace(self, args):
        if len(args) != 1:
            self.notify('output-workspace', 'Command must have a single argument', icon='dialog-error')
            return
        if not args[0].isdigit():
            self.notify('output-workspace', 'Argument must be an integer', icon='dialog-error')
            return
        index = int(args[0])
        if workspace := next((w for w in self.current_output.workspaces if (w.num % 10) == (index + 1)
                and w != self.current_workspace), None):
            return await self.i3.command(f'workspace {workspace.name}')
        if index < 0:
            index = 0
        elif index >= len(self.current_output.workspaces):
            index = len(self.current_output.workspaces) - 1
        workspace = self.current_output.workspaces[index]
        await self.i3.command(f'workspace {workspace.name}')

    async def workspace_tree(self):
        def find_workspace(node):
            if node.name == self.current_workspace.name:
                return node
            for subnode in node.nodes:
                if new_node := find_workspace(subnode):
                    return new_node

        def walk(node, level=0, end=False, trailing=()):
            body = ''
            indent = ''
            for i in range(level - 1):
                indent += '\u2502 ' if i in trailing else '  '
            if level:
                indent += '\u2514 ' if end else '\u251c '
            name = node.name or node.layout
            body_current = f'{indent}{node.type}: {name}\n'
            # Emphasize current window
            if node.focused:
                body_current = f'<b>{body_current}</b>'
            body += body_current
            total = len(node.nodes)
            _trailing = list(trailing) + [level]
            for i, subnode in enumerate(node.nodes, 1):
                end = i == total
                if end:
                    _trailing = trailing
                body += walk(subnode, level + 1, end=end, trailing=_trailing)
            return body
        root = await self.i3.get_tree()
        root = find_workspace(root)
        if not root:
            self.notify('Workspace tree', f'Cannot find root node for workspace {self.current_workspace.name}',
                    icon='dialog-error')
            return
        body = walk(root)
        self._last_tree_notification = self.notify('Workspace tree', body,
                replaces_id=self._last_tree_notification, timeout=60000)

    async def new_output_workspace(self):
        nums = set(workspace.num for workspace in self.current_output.workspaces)
        start = self.get_output_start(self.current_output)
        for i in itertools.count(start):
            if i in nums:
                continue
            await self.i3.command(f'workspace {i}')
            break

    # Helpers

    def get_output(self, con):
        if isinstance(con, str):
            name = con
        else:
            name = con.name
        output = self.outputs.get(name)
        if output is None:
            output = self.outputs[name] = Output(name)
        return output

    def get_workspace(self, con):
        key = (con.num, con.name)
        workspace = self.workspaces.get(key)
        if workspace is None:
            workspace = self.workspaces[key] = Workspace(con.num, con.name)
        return workspace

    def notify(self, summary, body, replaces_id=0, icon='', timeout=5000):
        return self.notifications.Notify('i3manager', replaces_id, icon, summary, body, [], [], timeout)

    def walk_nodes(self, node, f):
        if f(node):
            return True

        for subnode in node.nodes:
            if self.walk_nodes(subnode, f):
                return True
        for subnode in node.floating_nodes:
            if self.walk_nodes(subnode, f):
                return True

    def get_output_start(self, output):
        if cfg := config.outputs.get(output.name):
            return cfg.get('start', 1)
        return 1

    # Event handlers

    def on_workspace_init(self, i3, e):
        workspace = self.get_workspace(e.current)
        output = self.get_output(e.ipc_data['current']['output'])
        output.add_workspace(workspace)

    def on_workspace_focus(self, i3, e):
        # logger.debug('workspace focus %s %s -> %s %s', e.old.type, e.old.name, e.current.type, e.current.name)
        self.current_output = self.get_output(e.ipc_data['current']['output'])
        self.current_workspace = self.get_workspace(e.current)
        if e.old and e.old.name == '__i3_scratch':
            def scratch_window(node):
                self.scratchpad.on_window_focus(node)
                self.current_workspace.on_window_close(node)
            self.scratchpad.clear()
            self.walk_nodes(e.old, scratch_window)

    def on_workspace_empty(self, i3, e):
        logger.info('Removing workspace %s', e.current.name)
        output = self.get_output(e.ipc_data['current']['output'])
        workspace = self.get_workspace(e.current)
        output.remove_workspace(workspace)
        self.workspaces.pop((workspace.num, workspace.name), None)

    def on_workspace_rename(self, i3, e):
        if self.current_workspace is None:
            print(e, dir(e))
            print('old', e.old)
            print('current', e.current)
            logger.warning('Received rename but no current workspace for %s', e.current.name)
            return
        workspace = self.current_workspace
        logger.info('Renaming workspace %s to %s', workspace.name, e.current.name)
        self.workspaces.pop((workspace.num, workspace.name), None)
        workspace.num = e.current.num
        workspace.name = e.current.name
        self.workspaces[(workspace.num, workspace.name)] = workspace
        self.current_output._sort_workspaces()

    def on_window_fullscreen(self, i3, e):
        print('on_window_fullscreen', e)

    def on_window_focus(self, i3, e):
        # logger.debug('window focus %s %s %s', e.container.type, e.container.name, e.container.id)
        if self.current_workspace and not (self.scratchpad and self.scratchpad.has_container(e.container)) \
                and (not e.container or e.container.floating != 'user_on'):
            self.current_workspace.on_window_focus(e.container)

    def on_window_close(self, i3, e):
        if self.current_workspace:
            self.current_workspace.on_window_close(e.container)

    def on_window_move(self, i3, e):
        if e.container.type == 'floating_con':
            for workspace in self.workspaces.values():
                for node in e.container.nodes:
                    workspace.on_window_close(node)
            return

        if e.container.window or (e.container.nodes and e.container.nodes[0].window):
            asyncio.create_task(self._on_window_move(i3, e))

        # logger.debug('%s', e.ipc_data)
        # asyncio.create_task(self._init())

        # if e.container.scratchpad_state == 'changed':
        #     # logger.debug('scratchpad_state %s %s', repr(e.container), dir(e.container))
        #     hidden = e.ipc_data['container']['output'] == '__i3'
        #     for node in e.container.nodes:
        #         if node.window:
        #             if hidden:
        #                 logger.debug('hidden scratchpad window: %s %s', node.type, node.name)
        #             else:
        #                 logger.debug('visible scratchpad window: %s %s', node.type, node.name)

    async def _on_window_move(self, i3, e):
        def _walk_tree(node):
            if node.type == e.container.type and node.window and (node.window == w or getattr(node.window, 'id', None) == w):
                return True
            for subnode in node.nodes:
                if r := _walk_tree(subnode):
                    if r is True and node.type == 'workspace':
                        return node
                    return r
            for subnode in node.floating_nodes:
                if r := _walk_tree(subnode):
                    if r is True and node.type == 'workspace':
                        return node
                    return r

        if e.container.window:
            w = e.container.window
        elif e.container.nodes:
            w = e.container.nodes[0]
        elif e.container.id:
            w = e.container.id
        else:
            return

        root = await i3.get_tree()
        current_workspace = None
        for workspace in self.workspaces.values():
            if workspace.has_container(e.container):
                current_workspace = workspace
                break
        workspace = _walk_tree(root)
        if not workspace:
            logger.warning('No workspace found for %s', w)
            return
        new_workspace = self.get_workspace(workspace)
        if current_workspace != new_workspace:
            logger.debug('Container %s moved to workspace %s', e.container.name, new_workspace.name)
            current_workspace.on_window_close(e.container)
            new_workspace.on_window_focus(e.container)
