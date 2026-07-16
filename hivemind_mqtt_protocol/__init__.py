"""
HiveMind MQTT Network Protocol Plugin

Transports encrypted HiveMessage frames over an MQTT broker so that any
satellite — including embedded ESP32 devices — can ride an existing IoT/HA
MQTT bus rather than opening a dedicated WebSocket connection.

Topic layout
------------
Each satellite's api_key IS its topic identifier — it is unique per client,
and without the matching password/crypto key the payload ciphertext is useless.

    <prefix>/<api_key>/in      satellite → master  (master subscribes <prefix>/+/in)
    <prefix>/<api_key>/out     master → satellite
    <prefix>/<api_key>/status  retained LWT presence (online/offline)

Crypto
------
The MQTT payload IS the same encrypted HiveMessage frame that the WebSocket
transport sends.  The broker only ever sees ciphertext.  No extra encryption
layer is added here; hivemind-core's AES-GCM / RSA / PAKE handshake runs
unchanged inside the payload bytes.

Auth
----
Two layers, consistent with the design doc:
  1. Broker-level: MQTT username/password or TLS client-cert (config keys
     ``broker_username`` / ``broker_password`` / ``tls`` / ``cert``).
  2. HiveMind-level: the HELLO/HANDSHAKE in-payload exchange, identical to
     the WebSocket path.  The api_key IS the MQTT topic segment, so the
     master knows which DB record to look up as soon as the first frame
     arrives.  No separate credential handshake is needed at the MQTT layer.
"""

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt
from ovos_bus_client.session import Session
from ovos_utils.log import LOG
from poorman_handshake import PasswordHandShake

try:
    from hivemind_core.config import runtime_password_min_bits
except ImportError:  # released hivemind-core without the helper
    import os

    def runtime_password_min_bits():
        return 0.0 if os.environ.get("HIVEMIND_DISABLE_PASSWORD_STRENGTH_CHECK", "").strip().lower() in ("1", "true", "yes", "on") else 40.0

from hivemind_core.protocol import (
    HiveMindClientConnection,
    HiveMindListenerProtocol,
    HiveMindNodeType,
)
from hivemind_plugin_manager.protocols import ClientCallbacks, NetworkProtocol

_ONLINE = "online"
_OFFLINE = "offline"

_DEFAULT_IDLE_TIMEOUT = 300


@dataclass
class HiveMindMqttProtocol(NetworkProtocol):
    """MQTT broker-mediated network protocol for hivemind-core.

    Config keys (all optional, with defaults shown):
        broker_host        (str)  "localhost"
        broker_port        (int)  1883
        broker_username    (str)  None  — MQTT broker username (not the HiveMind key)
        broker_password    (str)  None  — MQTT broker password
        tls                (bool) False — enable TLS
        tls_ca_certs       (str)  None  — path to CA bundle
        tls_certfile       (str)  None  — path to client cert (mTLS)
        tls_keyfile        (str)  None  — path to client key  (mTLS)
        tls_insecure       (bool) False — explicitly disable verification
        health_file        (str)  None  — broker-ready marker for probes
        reconnect_min_delay (int) 1    — initial reconnect delay
        reconnect_max_delay (int) 30   — maximum reconnect delay
        topic_prefix       (str)  "hivemind"
        hub_id             (str)  None  — optional hub topic namespace
        client_id          (str)  None  — exact broker client id override
        client_id_suffix   (str)  $HOSTNAME — replica-safe suffix source
        qos                (int)  1
        idle_timeout       (int)  300   — seconds of silence before eviction; 0 disables
    """

    config: Dict[str, Any] = field(default_factory=dict)
    hm_protocol: Optional[HiveMindListenerProtocol] = None
    callbacks: ClientCallbacks = field(default_factory=ClientCallbacks)

    _peers: Dict[str, HiveMindClientConnection] = field(default_factory=dict, init=False, repr=False)
    _last_seen: Dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _mqtt: Optional[mqtt.Client] = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def _prefix(self) -> str:
        return str(self._cfg("topic_prefix") or "hivemind")

    def _hub_id(self) -> str:
        return str(self._cfg("hub_id") or "").strip().strip("/")

    def _topic_base(self) -> str:
        prefix = self._prefix().strip("/")
        hub_id = self._hub_id()
        return f"{prefix}/{hub_id}" if hub_id else prefix

    def _qos(self, is_bin: bool = False) -> int:
        if is_bin:
            return 0
        v = self._cfg("qos")
        return int(v) if v is not None else 1

    def _health_file(self) -> Optional[Path]:
        configured = str(self._cfg("health_file") or "").strip()
        return Path(configured) if configured else None

    def _set_broker_ready(self, ready: bool) -> None:
        marker = self._health_file()
        if marker is None:
            return
        try:
            if ready:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("ready\n", encoding="utf-8")
            else:
                marker.unlink(missing_ok=True)
        except OSError as exc:
            LOG.warning(f"[MQTT] Failed to update broker readiness marker: {exc}")

    # topic builders ---------------------------------------------------

    def in_topic(self, api_key: str) -> str:
        """Inbound topic: satellite → master."""
        if self._hub_id():
            return f"{self._topic_base()}/c2s/{api_key}"
        return f"{self._topic_base()}/{api_key}/in"

    def out_topic(self, api_key: str) -> str:
        """Outbound topic: master → satellite."""
        if self._hub_id():
            return f"{self._topic_base()}/s2c/{api_key}"
        return f"{self._topic_base()}/{api_key}/out"

    def status_topic(self, api_key: str) -> str:
        """Retained LWT presence topic."""
        if self._hub_id():
            return f"{self._topic_base()}/status/{api_key}"
        return f"{self._topic_base()}/{api_key}/status"

    def in_wildcard(self) -> str:
        if self._hub_id():
            return f"{self._topic_base()}/c2s/+"
        return f"{self._topic_base()}/+/in"

    def status_wildcard(self) -> str:
        if self._hub_id():
            return f"{self._topic_base()}/status/+"
        return f"{self._topic_base()}/+/status"

    def master_status_topic(self) -> str:
        return self.status_topic(self.identity.name or "master")

    def _broker_client_id(self) -> str:
        explicit = self._cfg("client_id")
        if explicit:
            return str(explicit)
        base = f"hivemind-{self._hub_id() or self.identity.name or 'master'}"
        suffix = self._cfg("client_id_suffix")
        if suffix is None:
            suffix = os.getenv("HOSTNAME")
        if not suffix:
            return base
        digest = hashlib.sha1(str(suffix).encode("utf-8")).hexdigest()[:10]
        return f"{base}-{digest}"

    # api_key extraction -----------------------------------------------

    @staticmethod
    def _api_key_from_topic(topic: str) -> Optional[str]:
        """Extract the api_key segment from <prefix>/<api_key>/<direction>."""
        parts = topic.split("/")
        if len(parts) >= 4 and parts[-2] in {"c2s", "s2c", "status"}:
            return parts[-1]
        if len(parts) >= 3:
            return parts[-2]
        return None

    @staticmethod
    def _direction_from_topic(topic: str) -> Optional[str]:
        parts = topic.split("/")
        if len(parts) >= 4 and parts[-2] in {"c2s", "s2c", "status"}:
            return parts[-2]
        if len(parts) >= 3:
            return parts[-1]
        return None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _build_client_connection(self, api_key: str) -> Optional[HiveMindClientConnection]:
        mqttclient = self._mqtt
        qos_fn = self._qos
        out = self.out_topic(api_key)
        status = self.status_topic(api_key)

        def do_send(payload: Any, is_bin: bool = False) -> None:
            if isinstance(payload, str):
                payload = payload.encode()
            mqttclient.publish(out, payload, qos=qos_fn(is_bin))

        def do_disconnect() -> None:
            mqttclient.publish(status, _OFFLINE, qos=1, retain=True)
            with self._lock:
                self._peers.pop(api_key, None)
                self._last_seen.pop(api_key, None)

        conn = HiveMindClientConnection(
            key=api_key,
            disconnect=do_disconnect,
            send_msg=do_send,
            sess=Session(session_id="default"),
            name=api_key,
            hm_protocol=self.hm_protocol,
        )

        self.hm_protocol.db.sync()
        user = self.hm_protocol.db.get_client_by_api_key(api_key)

        if not user:
            LOG.error(f"[MQTT] Invalid api_key in topic: {api_key!r}")
            self.hm_protocol.handle_invalid_key_connected(conn)
            return None

        conn.name = f"{api_key}::{user.client_id}::{user.name}"
        conn.crypto_key = user.crypto_key
        conn.skill_blacklist = user.skill_blacklist or []
        conn.intent_blacklist = user.intent_blacklist or []
        conn.allowed_types = user.allowed_types
        conn.can_broadcast = user.can_broadcast
        conn.can_propagate = user.can_propagate
        conn.can_escalate = user.can_escalate
        conn.is_admin = user.is_admin
        if user.password:
            conn.pswd_handshake = PasswordHandShake(user.password, min_bits=runtime_password_min_bits())

        conn.node_type = HiveMindNodeType.NODE

        if (
            not conn.crypto_key
            and not self.hm_protocol.handshake_enabled
            and self.hm_protocol.require_crypto
        ):
            LOG.error("[MQTT] No crypto key and handshake disabled but require_crypto=True")
            self.hm_protocol.handle_invalid_protocol_version(conn)
            return None

        with self._lock:
            self._peers[api_key] = conn
            self._last_seen[api_key] = time.monotonic()

        self.hm_protocol.handle_new_client(conn)
        LOG.info(f"[MQTT] New connection: {conn.name!r}")
        return conn

    def _disconnect_peer(self, api_key: str) -> None:
        with self._lock:
            conn = self._peers.pop(api_key, None)
            self._last_seen.pop(api_key, None)
        if conn is not None:
            LOG.info(f"[MQTT] Disconnecting peer {api_key!r}")
            self.hm_protocol.handle_client_disconnected(conn)

    # ------------------------------------------------------------------
    # paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, rc: int) -> None:
        if rc != 0:
            self._set_broker_ready(False)
            LOG.error(f"[MQTT] Broker connection failed, rc={rc}")
            return
        LOG.info("[MQTT] Connected to broker")
        client.subscribe(self.in_wildcard(), qos=self._qos())
        client.subscribe(self.status_wildcard(), qos=1)
        client.publish(self.master_status_topic(), _ONLINE, qos=1, retain=True)
        self._set_broker_ready(True)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        topic: str = msg.topic
        payload: bytes = msg.payload

        parts = topic.split("/")
        if len(parts) < 3:
            LOG.warning(f"[MQTT] Unexpected topic shape: {topic!r}")
            return

        direction = self._direction_from_topic(topic)
        api_key = self._api_key_from_topic(topic)
        if not direction or not api_key:
            LOG.warning(f"[MQTT] Unexpected topic shape: {topic!r}")
            return

        if direction == "status":
            # The master publishes its own presence to
            # <prefix>/<master_name>/status, which also matches its
            # <prefix>/+/status subscription. Ignore that self-echo so the
            # master never tries to treat itself as a satellite peer.
            if api_key == (self.identity.name or "master"):
                return
            status_val = payload.decode(errors="replace").strip()
            if status_val == _OFFLINE:
                LOG.info(f"[MQTT] LWT offline for {api_key!r}")
                self._disconnect_peer(api_key)
            return

        if direction not in {"in", "c2s"}:
            return

        with self._lock:
            conn = self._peers.get(api_key)
            if conn is not None:
                self._last_seen[api_key] = time.monotonic()

        if conn is None:
            conn = self._build_client_connection(api_key)
            if conn is None:
                return

        try:
            message = conn.decode(self._coerce_payload(payload))
        except Exception as e:
            LOG.warning(f"[MQTT] Failed to decode frame from {api_key!r}: {e}")
            return

        self.hm_protocol.handle_message(message, conn)

    @staticmethod
    def _coerce_payload(payload: bytes):
        """Return the payload typed the way ``HiveMindClientConnection.decode``
        expects.

        MQTT delivers every payload as ``bytes``, but ``decode`` treats any
        ``bytes`` value as a binary *bitstring* frame and any ``str`` value as
        a JSON frame (plaintext handshake or AES-GCM ciphertext-JSON). Text
        HiveMessage frames — the default, non-binarized path used by the
        handshake and ordinary BUS messages — are valid UTF-8 JSON objects, so
        decode them back to ``str``; anything that is not valid UTF-8 JSON is a
        genuine binary frame and is passed through as ``bytes``.
        """
        if not isinstance(payload, (bytes, bytearray)):
            return payload
        stripped = payload.lstrip()
        if stripped[:1] in (b"{", b"["):
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                return bytes(payload)
        return bytes(payload)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        self._set_broker_ready(False)
        if rc != 0:
            LOG.warning(f"[MQTT] Unexpected broker disconnect, rc={rc}")

    # ------------------------------------------------------------------
    # Idle-timeout sweep
    # ------------------------------------------------------------------

    def _idle_sweep(self, idle_timeout: float) -> None:
        while True:
            time.sleep(max(idle_timeout / 4, 30))
            now = time.monotonic()
            with self._lock:
                stale = [k for k, ts in list(self._last_seen.items()) if (now - ts) > idle_timeout]
            for key in stale:
                LOG.info(f"[MQTT] Idle timeout for peer {key!r}")
                self._disconnect_peer(key)

    # ------------------------------------------------------------------
    # run()
    # ------------------------------------------------------------------

    def run(self) -> None:
        broker_host: str = str(self._cfg("broker_host") or "localhost")
        broker_port: int = int(self._cfg("broker_port") or 1883)
        LOG.debug(
            f"[MQTT] protocol configured for broker={broker_host}:{broker_port}, "
            f"tls={bool(self._cfg('tls', False))}"
        )
        self._set_broker_ready(False)

        self._mqtt = mqtt.Client(client_id=self._broker_client_id())

        username: Optional[str] = self._cfg("broker_username")
        password: Optional[str] = self._cfg("broker_password")
        if username:
            self._mqtt.username_pw_set(username, password)

        if self._cfg("tls", False):
            self._mqtt.tls_set(
                ca_certs=self._cfg("tls_ca_certs"),
                certfile=self._cfg("tls_certfile"),
                keyfile=self._cfg("tls_keyfile"),
            )
            if self._cfg("tls_insecure", False):
                LOG.warning("[MQTT] TLS certificate verification is explicitly disabled")
                self._mqtt.tls_insecure_set(True)

        reconnect_min = max(1, int(self._cfg("reconnect_min_delay", 1)))
        reconnect_max = max(reconnect_min, int(self._cfg("reconnect_max_delay", 30)))
        self._mqtt.reconnect_delay_set(min_delay=reconnect_min, max_delay=reconnect_max)

        master_status = self.master_status_topic()
        self._mqtt.will_set(master_status, _OFFLINE, qos=1, retain=True)

        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message
        self._mqtt.on_disconnect = self._on_disconnect

        self._mqtt.connect_async(broker_host, broker_port, keepalive=60)

        # Missing/None → default; any value <= 0 disables the sweep entirely.
        raw_idle = self._cfg("idle_timeout")
        idle_timeout = float(raw_idle if raw_idle is not None else _DEFAULT_IDLE_TIMEOUT)
        if idle_timeout > 0:
            threading.Thread(
                target=self._idle_sweep, args=(idle_timeout,),
                daemon=True, name="mqtt-idle-sweep",
            ).start()

        LOG.info(f"[MQTT] listener started — broker={broker_host}:{broker_port}")
        try:
            self._mqtt.loop_forever(retry_first_connection=True)
        finally:
            self._set_broker_ready(False)
