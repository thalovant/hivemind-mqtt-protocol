"""In-process MQTT broker double for end-to-end tests.

No external mosquitto, no sockets, no network — :class:`FakeMqttBroker`
implements just enough of the MQTT broker semantics that the *real*
``HiveMindMqttProtocol`` (and a real satellite-side client) exercise the
genuine publish / subscribe / wildcard / retained / LWT code paths.

The pieces:

* :class:`FakeMqttBroker` — the router. Tracks subscriptions (with ``+``
  wildcard matching via paho's own ``topic_matches_sub``), retained
  messages, and registered last-will testaments. Delivery is synchronous
  and in-thread, which makes round-trips deterministic.

* :class:`FakeMqttClient` — a drop-in for ``paho.mqtt.client.Client`` that
  speaks to a :class:`FakeMqttBroker` instead of a TCP socket. It mirrors
  the subset of the paho v1 callback API that the protocol and the test
  satellite use: ``connect``, ``subscribe``, ``publish``, ``will_set``,
  ``username_pw_set``, ``tls_set``, ``loop_start`` / ``loop_forever`` /
  ``loop_stop`` / ``disconnect`` and the ``on_connect`` / ``on_message`` /
  ``on_disconnect`` callbacks.

The master under test obtains its client by having
``paho.mqtt.client.Client`` patched to ``broker.make_client``; nothing in
``hivemind_mqtt_protocol`` is altered.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from paho.mqtt.client import topic_matches_sub


class _Subscription:
    __slots__ = ("client", "filter", "qos")

    def __init__(self, client: "FakeMqttClient", topic_filter: str, qos: int):
        self.client = client
        self.filter = topic_filter
        self.qos = qos


class FakeMqttBroker:
    """A minimal, synchronous, in-process MQTT broker.

    Implements topic wildcard routing (``+`` single-level), retained
    messages, and last-will-and-testament delivery on ungraceful
    disconnect — the three broker behaviours the HiveMind MQTT topic scheme
    relies on.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: List[_Subscription] = []
        self._retained: Dict[str, Tuple[bytes, int]] = {}
        self._wills: Dict["FakeMqttClient", Tuple[str, bytes, int, bool]] = {}
        # Flat log of every published (topic, payload) for assertions.
        self.published: List[Tuple[str, bytes, bool]] = []

    # -- client factory ------------------------------------------------

    def make_client(self, *args: Any, **kwargs: Any) -> "FakeMqttClient":
        """Factory matching ``paho.mqtt.client.Client(...)``.

        Accepts (and ignores) the positional/keyword args paho takes so it
        can stand in directly when ``paho.mqtt.client.Client`` is patched.
        """
        return FakeMqttClient(self)

    # -- registration --------------------------------------------------

    def _register_will(self, client: "FakeMqttClient",
                       topic: str, payload: bytes, qos: int, retain: bool) -> None:
        with self._lock:
            self._wills[client] = (topic, payload, qos, retain)

    def _subscribe(self, client: "FakeMqttClient", topic_filter: str, qos: int) -> None:
        with self._lock:
            self._subs.append(_Subscription(client, topic_filter, qos))
            # Replay retained messages whose topic matches the new filter.
            matches = [
                (t, p) for t, (p, _q) in self._retained.items()
                if topic_matches_sub(topic_filter, t)
            ]
        for topic, payload in matches:
            client._deliver(topic, payload)

    def _publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        elif payload is None:
            payload = b""
        with self._lock:
            self.published.append((topic, payload, retain))
            if retain:
                if payload == b"":
                    self._retained.pop(topic, None)
                else:
                    self._retained[topic] = (payload, qos)
            targets = [s.client for s in self._subs
                       if topic_matches_sub(s.filter, topic)]
        # Deliver outside the lock so handlers may publish re-entrantly.
        for client in targets:
            client._deliver(topic, payload)

    def _disconnect(self, client: "FakeMqttClient", *, graceful: bool) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s.client is not client]
            will = self._wills.pop(client, None)
        # An ungraceful disconnect fires the registered LWT.
        if will is not None and not graceful:
            topic, payload, qos, retain = will
            self._publish(topic, payload, qos, retain)

    # -- assertions helpers --------------------------------------------

    def retained(self, topic: str) -> Optional[bytes]:
        with self._lock:
            entry = self._retained.get(topic)
            return entry[0] if entry else None


class FakeMqttClient:
    """A ``paho.mqtt.client.Client`` look-alike backed by a broker.

    Only the subset of the API the protocol (and the test satellite) use is
    implemented; everything else is intentionally absent so accidental
    reliance on un-modelled behaviour surfaces as ``AttributeError``.
    """

    def __init__(self, broker: FakeMqttBroker) -> None:
        self._broker = broker
        self._connected = False
        # paho v1 callbacks
        self.on_connect: Optional[Callable] = None
        self.on_message: Optional[Callable] = None
        self.on_disconnect: Optional[Callable] = None
        self._will: Optional[Tuple[str, bytes, int, bool]] = None
        self._userdata: Any = None
        self._stop = threading.Event()
        # Inbound messages are queued and dispatched on a background thread,
        # mirroring paho's network loop. This keeps publish() non-re-entrant:
        # a handler that publishes does not synchronously re-enter on_message.
        self._inbox: "queue.Queue[Optional[Tuple[str, bytes]]]" = queue.Queue()
        self._dispatch_thread: Optional[threading.Thread] = None
        self._async_connect: Optional[Tuple[str, int, int]] = None

    # -- configuration mirrors -----------------------------------------

    def username_pw_set(self, username: str, password: Optional[str] = None) -> None:
        self.username = username
        self.password = password

    def tls_set(self, ca_certs=None, certfile=None, keyfile=None, **_: Any) -> None:
        self.tls = {"ca_certs": ca_certs, "certfile": certfile, "keyfile": keyfile}

    def tls_insecure_set(self, value: bool) -> None:
        self.tls_insecure = value

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        self.reconnect_delays = (min_delay, max_delay)

    def will_set(self, topic: str, payload=None, qos: int = 0, retain: bool = False) -> None:
        self._will = (topic, payload, qos, retain)

    def user_data_set(self, userdata: Any) -> None:
        self._userdata = userdata

    # -- connection ----------------------------------------------------

    def connect(self, host: str, port: int = 1883, keepalive: int = 60) -> int:
        self.host, self.port, self.keepalive = host, port, keepalive
        self._connected = True
        if self._will is not None:
            self._broker._register_will(self, *self._will)
        # Start the background dispatch loop (paho's network loop equivalent).
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="fake-mqtt-dispatch")
        self._dispatch_thread.start()
        # paho fires on_connect from the network loop; emulate that here so
        # subscriptions registered in on_connect take effect immediately.
        if self.on_connect is not None:
            self.on_connect(self, self._userdata, {}, 0)
        return 0

    def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> int:
        self._async_connect = (host, port, keepalive)
        return 0

    def _dispatch_loop(self) -> None:
        while True:
            item = self._inbox.get()
            if item is None:  # sentinel: stop
                return
            topic, payload = item
            if self.on_message is not None:
                try:
                    self.on_message(self, self._userdata, _Message(topic, payload))
                except Exception:
                    pass

    def reconnect(self) -> int:
        return self.connect(self.host, self.port, self.keepalive)

    # -- pub/sub -------------------------------------------------------

    def subscribe(self, topic: str, qos: int = 0):
        self._broker._subscribe(self, topic, qos)
        return (0, 1)

    def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False):
        self._broker._publish(topic, payload, qos, retain)
        return _PublishResult()

    def _deliver(self, topic: str, payload: bytes) -> None:
        # Enqueue for the background dispatch thread rather than calling
        # on_message inline; this matches real MQTT (delivery happens on the
        # network loop, not the publisher's stack).
        self._inbox.put((topic, payload))

    # -- loops ---------------------------------------------------------

    def loop_start(self) -> None:
        # The dispatch thread (started in connect) is the network loop.
        pass

    def loop_stop(self) -> None:
        pass

    def loop_forever(self, retry_first_connection: bool = False) -> None:
        del retry_first_connection
        if not self._connected and self._async_connect is not None:
            self.connect(*self._async_connect)
        # Block until disconnect/stop so the protocol's run() thread can sit
        # here exactly as it would against a real broker.
        self._stop.wait()

    def _shutdown(self, *, graceful: bool) -> None:
        self._connected = False
        self._stop.set()
        self._inbox.put(None)  # stop the dispatch loop
        self._broker._disconnect(self, graceful=graceful)

    def disconnect(self) -> int:
        self._shutdown(graceful=True)
        if self.on_disconnect is not None:
            self.on_disconnect(self, self._userdata, 0)
        return 0

    def force_will(self) -> None:
        """Simulate an ungraceful drop so the broker fires this client's LWT."""
        self._shutdown(graceful=False)


class _PublishResult:
    """Stand-in for paho's MQTTMessageInfo."""
    rc = 0
    mid = 1

    def wait_for_publish(self, timeout: Optional[float] = None) -> None:
        pass

    def is_published(self) -> bool:
        return True


class _Message:
    """Stand-in for paho's MQTTMessage as seen by on_message."""
    __slots__ = ("topic", "payload", "qos", "retain")

    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload
        self.qos = 0
        self.retain = False
