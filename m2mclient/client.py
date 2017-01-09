import weakref
import logging
from threading import Event
from threading import Thread

from ws4py.client import WebSocketBaseClient

from .dispatcher import Dispatcher
from .dispatcher import PacketFormatError
from .dispatcher import expose
from .packets import M2MPacket
from .packets import PacketType
from . import errors


log = logging.getLogger('m2m')



class WebSocket(WebSocketBaseClient):
    """Websocket thread."""

    def __init__(self, url, dispatcher, on_startup=None):
        self._dispatcher = weakref.ref(dispatcher)
        self.on_startup = on_startup or (lambda: None)
        self.running = False
        self.ready_event = Event()
        self.exc = None
        super().__init__(url)

    @property
    def dispatcher(self):
        return self._dispatcher()

    def start(self):
        self._thread = Thread(target=self.run_thread)
        self._thread.daemon = True
        self._thread.start()

    def join(self, timeout=None):
        """Attempt to join thread, return True if joined."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def run_thread(self):
        try:
            try:
                self.connect()
            except Exception as error:
                log.warning(error)
                self.exc = error
                self.ready_event.set()
            else:
                self.running = True
                self.on_startup()
                self.ready_event.set()
                self.run()
        finally:
            self.running = False

    def received_message(self, msg):
        if not isinstance(msg.data, bytes):
            return
        if not self.dispatcher:
            return
        try:
            packet = M2MPacket.from_bytes(msg.data)
        except PacketFormatError as packet_error:
            # We received a badly formatted packet from the server
            # Inconceivable!
            log.warning('bad packet (%s)', packet_error)
        else:
            log.debug(' <- %r', packet)
            self.dispatcher.dispatch_packet(packet)



class CommandResult(object):
    """
    A pending result that may block until a response is received from
    the server.

    """

    def __init__(self, name):
        self.name = name
        self._result = None
        self._event = Event()

    def __repr__(self):
        return "CommandResult({!r})".format(self.name)

    def set(self, result):
        """Set the result from another thread."""
        self._result = result
        self._event.set()

    def set_fail(self, result):
        """Set a fail result."""
        #
        self._result = result
        self._event.set()

    def get(self, timeout=5):
        """Get the result or throw a CommandTimeout error.

        In normal operation this should return in less than a second.
        Timeouts could occur if the m2m server is down, overloaded, or
        otherwise fubar.
        """
        # The default timeout of 5 seconds is probably unrealistically
        # high Even under load the server response time should be
        # measured in milliseconds
        if not self._event.wait(timeout):
            raise errors.CommandTimeout('command timed out')
        if self._result is None:
            raise errors.CommandError(
                'no result available (connection closed before it was received)'
            )
        if not isinstance(self._result, dict):
            raise errors.CommandFail('invalid response')
        status = self._result.get('status', 'fail')
        if status != 'ok':
            msg = self._result.get('msg', '')
            raise errors.CommandFail("{}; {}".format(status, msg))
        return self._result


class M2MClient:
    """A client for the M2M protocol."""

    def __init__(self, url, username, password, connect_wait=3):
        self.url = url
        self.username = username
        self.password = password
        self.connect_wait = connect_wait
        self.identity = None
        self.dispatcher = Dispatcher(M2MPacket, instance=self)
        self.command_id = 0
        self.command_events = {}
        self.ws = None
        self.create_ws()

    def create_ws(self):
        self.ws = WebSocket(
            self.url,
            self.dispatcher,
            on_startup=self.on_startup
        )
        self.ws.sock.settimeout(3)

    def __enter__(self):
        log.debug('connecting to %s', self.url)
        self.ws.start()
        self.ws.ready_event.wait(self.connect_wait)
        if not self.ws.running:
            raise errors.ConnectionError(
                str(self.ws.exc)
                if self.ws.exc
                else "unable to connect"
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            # Ask politely to leave
            self.close()
            # Wait until we're done
            if not self.ws.join(1):
                # Force a close if we didn't complete
                self.ws.close()
        finally:
            self.ws = None
            self.dispatcher.close()

    def close(self):
        """A graceful close."""
        # If everything is working, the server will kick us in a few
        # milliseconds.
        self.send('request_leave')

    def send(self, packet_type, *args, **kwargs):
        """Send a packet."""
        packet = M2MPacket.create(packet_type, *args, **kwargs)
        if self.ws.running:
            self.ws.send(packet.as_bytes, binary=True)
            log.debug(' -> %r', packet)
        else:
            log.debug(' -> %r (server gone)', packet)

    def command(self, command_packet, *args, **kwargs):
        """
        Send a command to the server.

        Return a CommandResult object that may be waited on.
        """
        command_id = self.command_id = self.command_id + 1
        result = self.command_events[command_id] = CommandResult(command_packet)
        self.send(command_packet, command_id, *args, **kwargs)
        return result

    def on_startup(self):
        """Called on startup."""
        self.send('request_join')
        self.send(
            'request_login',
            username=self.username,
            password=self.password
        )

    def log(self, text):
        """Broadcast a log message."""
        text_bytes = text.encode()
        return self.command('command_broadcast_log', text=text_bytes)

    def add_route(self, node1, node2):
        """Create a single route."""
        result = self.command(
            "command_add_route",
            node1=node1,
            port1=-1,
            node2=node2,
            port2=-1,
            requester=self.identity or b'',
            forwarded=0
        )
        return result

    def send_instruction(self, node, **params):
        """Send an instruction to the client."""
        result = self.command('command_send_instruction',
                              node=node,
                              data=params)
        return result

    def name_node(self, node, name):
        """Associate a node (UUID) with a name."""
        return self.command("command_set_name",
                            node=node,
                            name=name)

    def get_identities(self, nodes):
        """Get identities of online nodes."""
        return self.command("command_get_identities", nodes=nodes)

    def set_meta(self, device_id, key, value):
        """Set meta information associated with a device."""
        result = self.command("command_set_meta",
                              requester=self.identity or b'',
                              node=device_id,
                              key=key,
                              value=value)
        return result

    def get_meta(self, device_id):
        """Get a meta dictionary associated with the device."""
        result = self.command("command_get_meta",
                              requester=self.identity or b'',
                              node=device_id)
        return result

    @expose(PacketType.response)
    def on_command(self, command_id, result):
        """Handle a response to a command."""
        try:
            command_result = self.command_events.pop(command_id)
        except KeyError:
            log.warning('received a response to an unknown event')
        else:
            if result.get(b'status') == b'ok':
                command_result.set(result)
            else:
                command_result.set_fail(result)

    @expose(PacketType.set_identity)
    def handle_set_identitiy(self, identity):
        """The server is informing us of our identity on the network."""
        self.identity = identity

    @expose(PacketType.welcome)
    def handle_welcome(self):
        """We can now open channels."""

    @expose(PacketType.notify_login_success)
    def handle_notify_login_success(self, user: bytes.decode):
        """Logged in ok."""

    @expose(PacketType.notify_login_fail)
    def handle_notify_login_fail(self, message: bytes.decode):
        """Username or password was wrong."""
        raise errors.M2MAuthFailed(message)

    @expose(PacketType.log)
    def handle_log(self, text: bytes.decode):
        log.info('[log] %s', text)