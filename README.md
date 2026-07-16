# hivemind-mqtt-protocol

An MQTT broker-mediated network protocol plugin for [hivemind-core](https://github.com/JarbasHiveMind/hivemind-core).

Satellites connect to the same MQTT broker they already use for sensors (Home
Assistant, ESPHome, Tasmota, ESP32) and exchange encrypted `HiveMessage` frames
over that broker.  No bespoke inbound port or WebSocket stack is required on the
hub; both hub and satellites are broker clients.

## The broker-mediated model

```
satellite ──pub──▶  broker  ◀──sub── hub
hub       ──pub──▶  broker  ◀──sub── satellite
```

The hub runs ONE paho-mqtt client.  Logical per-satellite connections are
derived from the topic hierarchy.

### Topic scheme

```
<prefix>/<api_key>/in      # satellite → master, legacy standalone layout
<prefix>/<api_key>/out     # master → satellite, legacy standalone layout
<prefix>/<api_key>/status  # retained LWT presence, legacy standalone layout

<prefix>/<hub_id>/c2s/<api_key>     # satellite → master, managed hub layout
<prefix>/<hub_id>/s2c/<api_key>     # master → satellite, managed hub layout
<prefix>/<hub_id>/status/<api_key>  # retained LWT presence, managed hub layout
```

Defaults: `prefix = hivemind`. Each satellite's HiveMind access key (`api_key`)
is its own topic segment — it is unique per client and identifies which DB
record to look up as soon as the first frame arrives. When `hub_id` is
configured, topics are scoped under that hub so managed broker ACLs can grant
one hub-owned topic tree.

The master also publishes its own presence. Standalone mode uses
`<prefix>/<master_name>/status`; managed mode uses
`<prefix>/<hub_id>/status/<master_name>`, keeping the Last Will inside the
hub-owned ACL tree. The master ignores its retained self-echo.

## Crypto

The MQTT payload carries the **same encrypted HiveMessage frame** the WebSocket
transport sends.  The broker only ever sees ciphertext.  HiveMind's full
AES-GCM / RSA / PAKE handshake runs unchanged inside the payload.

## Authentication

Two independent layers:

1. **Broker-level** — MQTT `username` / `password`, or TLS client-cert.
   Configure the broker's ACL so each satellite may only publish to its own
   `<api_key>/in` topic and subscribe to its own `<api_key>/out` topic.

2. **HiveMind-level** — the HELLO / HANDSHAKE exchange embedded in the
   encrypted payload, identical to the WebSocket path.  The `api_key` IS the
   topic segment, so the master knows which DB record to look up as soon as the
   first frame arrives.

## QoS

| Traffic type | QoS |
|---|---|
| Control frames (default) | 1 (at-least-once) |
| Binary / audio frames | 0 (fire-and-forget, low latency) |

## Configuration keys

| Key | Default | Description |
|---|---|---|
| `broker_host` | `localhost` | MQTT broker hostname or IP |
| `broker_port` | `1883` | Broker port (8883 for TLS) |
| `broker_username` | — | MQTT broker username for the master |
| `broker_password` | — | MQTT broker password for the master |
| `tls` | `false` | Enable TLS |
| `tls_ca_certs` | — | Path to CA bundle |
| `tls_certfile` | — | Path to client cert (mTLS) |
| `tls_keyfile` | — | Path to client key (mTLS) |
| `tls_insecure` | `false` | Skip broker certificate verification for trusted internal brokers |
| `topic_prefix` | `hivemind` | Topic namespace prefix |
| `hub_id` | — | Optional hub namespace for managed broker ACLs |
| `qos` | `1` | Default MQTT QoS for control frames |
| `idle_timeout` | `300` | Seconds of silence before evicting a peer (0 = off) |
| `client_id` | — | Explicit broker client id for special deployments |
| `client_id_suffix` | `$HOSTNAME` | Replica-specific suffix hashed into the default broker client id |

## Usage

```python
from hivemind_plugin_manager import NetworkProtocolFactory

server = NetworkProtocolFactory.create(
    "hivemind-mqtt-plugin",
    config={
        "broker_host": "192.168.1.100",
        "broker_port": 1883,
    },
)
server.run()   # blocks
```

## Satellite side

A satellite is any MQTT client that:

1. sets a retained LWT `offline` on `<prefix>/<api_key>/status`, connects, and
   publishes a retained `online` there;
2. subscribes to `<prefix>/<api_key>/out`;
3. publishes its first HiveMind frame to `<prefix>/<api_key>/in` — this is what
   makes the master create the logical connection and reply (over the `out`
   topic) with its `HELLO` + handshake request, after which the satellite runs
   the normal HiveMind handshake and then exchanges encrypted frames.

A reference satellite that drives a real `HiveMindSlaveProtocol` over MQTT lives
in the end-to-end test harness (`tests/e2e/mqtt_satellite.py`); see
[Testing](#testing). A first-class transport option in `hivemind-bus-client` (or
a dedicated `hivemind-mqtt-client`) and an ESPHome / Tasmota external-component
example for ESP32 satellites are planned follow-ups.

## Testing

```bash
pip install -e .[e2e]   # installs hivescope + the in-process harness
pytest tests/           # unit + end-to-end
```

- `tests/test_mqtt_protocol.py` — unit tests (topic/routing/lifecycle logic,
  mocked paho client and `hm_protocol`).
- `tests/e2e/` — end-to-end tests that run the **real** `HiveMindMqttProtocol`
  master and a **real** `HiveMindSlaveProtocol` satellite, exercising the full
  loop (handshake → encrypted BUS round-trip → LWT presence). The MQTT broker is
  an in-process double (`tests/e2e/broker.py`) — no external mosquitto, no
  sockets, no network are required.

## Where it fits

```
hivemind-core
  └── hivemind-plugin-manager  (NetworkProtocolFactory loads plugins by entry-point)
        └── hivemind-mqtt-protocol  ← this repo
              └── paho-mqtt client connected to an external MQTT broker
```

The plugin registers under the `hivemind.network.protocol` entry-point group as
`hivemind-mqtt-plugin`.

## Docs

- [docs/architecture.md](docs/architecture.md) — topic scheme, crypto, QoS, idle eviction
- [docs/configuration.md](docs/configuration.md) — full configuration reference
- [docs/operations.md](docs/operations.md) — broker setup, TLS/mTLS, authoring a transport plugin

## Install

```bash
pip install hivemind-mqtt-protocol
```
