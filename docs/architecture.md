# Architecture

## Class hierarchy

```
hivemind_plugin_manager.protocols.NetworkProtocol  (abstract)
        │
        └─ hivemind_mqtt_protocol.HiveMindMqttProtocol
                │
                └─ paho.mqtt.client.Client (ONE broker connection for the hub)
```

`HiveMindMqttProtocol.run()` is the blocking server entry point called by
`hivemind-core`. It connects a single paho-mqtt client to the external broker
and subscribes to the wildcard topic for incoming satellite messages.

## Broker-mediated topology

```
satellite ──pub──▶  broker  ◀──sub── hub
hub       ──pub──▶  broker  ◀──sub── satellite
```

The hub does not bind any TCP port. Both hub and satellites are broker
**clients**. This means:

- No inbound firewall rule is needed on the hub.
- Any satellite that can reach the broker can reach the hub.
- The broker handles delivery, buffering (QoS 1), and presence (LWT).

## Topic scheme

```
<prefix>/<api_key>/in      # satellite → master, legacy standalone layout
<prefix>/<api_key>/out     # master → satellite, legacy standalone layout
<prefix>/<api_key>/status  # retained LWT presence, legacy standalone layout

<prefix>/<hub_id>/c2s/<api_key>     # satellite → master, managed hub layout
<prefix>/<hub_id>/s2c/<api_key>     # master → satellite, managed hub layout
<prefix>/<hub_id>/status/<api_key>  # retained LWT presence, managed hub layout
```

Defaults: `prefix = hivemind`.

The `api_key` segment is the satellite's HiveMind access key. It is unique per
client, so the master can look up the matching DB record from the topic as soon
as the first frame arrives; without the matching crypto key the payload
ciphertext remains useless.

When `hub_id` is configured, the hub uses the managed layout so broker ACLs can
grant the hub one bounded topic tree.

## Crypto

The MQTT payload carries the **same encrypted HiveMessage frame** that the
WebSocket transport sends. The broker sees only ciphertext. HiveMind's full
AES-GCM / RSA / PAKE handshake runs unchanged inside the payload bytes.

No additional encryption layer is added by this transport.

## Wire format (text vs binary frames)

A `HiveMessage` frame is either:

- a **text** frame — a UTF-8 JSON object (the default, non-binarized path: the
  plaintext handshake bootstrap and AES-GCM ciphertext-JSON for everything
  after); or
- a **binary** frame — a packed bitstring, used for the binarized / audio path.

WebSocket preserves this text/binary distinction natively; MQTT does not — every
MQTT payload is opaque `bytes`. On receive, the master therefore inspects the
payload: a value that begins with `{` or `[` and is valid UTF-8 is decoded back
to `str` (so `HiveMindClientConnection.decode` takes its JSON path); anything
else is passed through as `bytes` and decoded as a binary bitstring frame.

## Connection lifecycle

MQTT has no connection event the master can hook, so there is no `accept()`
loop. A logical per-satellite connection is created lazily on the **first
inbound frame** on `<prefix>/<api_key>/in`:

1. The satellite announces presence (retained LWT + `online`), subscribes to its
   `out` topic, and publishes its first frame to its `in` topic.
2. The master's single client receives it on `<prefix>/+/in`, looks up the
   `api_key` in the DB, builds the `HiveMindClientConnection`, and (via
   `handle_new_client`) replies on the `out` topic with `HELLO` + a handshake
   request.
3. The satellite completes one HiveMind handshake; both sides derive the same
   session key, and encrypted frames flow in both directions.

Because the master initiates the handshake (step 2), the satellite must **not**
start its own handshake before that first round-trip — doing so would race the
master's request and derive a mismatched key.

### Master self-presence

The master publishes its own presence to `<prefix>/<master_name>/status` in
standalone mode and `<prefix>/<hub_id>/status/<master_name>` in managed mode.
The managed Last Will therefore remains inside the hub-owned ACL tree. The
master recognises and ignores its retained self-echo.

## Authentication layers

1. **Broker-level**: MQTT `username` / `password` (config keys `broker_username` /
   `broker_password`), or TLS client-cert (config keys `tls_certfile` /
   `tls_keyfile`). Configure the broker's ACL so each satellite may only publish
   to its own `<api_key>/in` topic and subscribe to its own `<api_key>/out` topic.

2. **HiveMind-level**: the HELLO / HANDSHAKE exchange embedded in the encrypted
   payload, identical to the WebSocket path. The `api_key` IS the topic segment,
   so the master can look up the DB record on first contact without a separate
   MQTT-layer credential handshake.

## QoS

| Traffic type | QoS | Rationale |
|---|---|---|
| Control frames (default) | 1 (at-least-once) | Delivery guarantee for messages. |
| Binary / audio frames | 0 (fire-and-forget) | Low latency; re-transmission of audio is worse than a gap. |

## Idle eviction

Satellites that send no messages for `idle_timeout` seconds are evicted
(treated as disconnected). Their LWT `<api_key>/status` topic is
checked on eviction. Set `idle_timeout: 0` to disable eviction.

Default: 300 seconds.

## Authoring a transport plugin

See [hivemind-websocket-protocol: authoring a transport plugin](https://github.com/JarbasHiveMind/hivemind-websocket-protocol/blob/dev/docs/architecture.md#authoring-a-transport-plugin)
for the `NetworkProtocol` ABC and entry-point registration pattern.
