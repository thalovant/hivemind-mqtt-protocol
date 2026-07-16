# Configuration Reference

All settings are passed in the `hivemind-mqtt-plugin` block of
`~/.config/hivemind-core/server.json`.

| Key | Default | Description |
|---|---|---|
| `broker_host` | `localhost` | MQTT broker hostname or IP. |
| `broker_port` | `1883` | Broker port (use `8883` for TLS). |
| `broker_username` | — | MQTT broker username for the master. |
| `broker_password` | — | MQTT broker password for the master. |
| `tls` | `false` | Enable TLS. |
| `tls_ca_certs` | — | Path to CA bundle for broker TLS verification. |
| `tls_certfile` | — | Path to client certificate (mTLS). |
| `tls_keyfile` | — | Path to client key (mTLS). |
| `tls_insecure` | `false` | Disable certificate verification only when explicitly required for a controlled test environment. |
| `topic_prefix` | `hivemind` | Topic namespace prefix. |
| `qos` | `1` | Default MQTT QoS for control frames. |
| `idle_timeout` | `300` | Seconds of silence before evicting a peer (0 = off). |
| `health_file` | — | File created only while the broker connection is established; suitable for container readiness probes. |
| `reconnect_min_delay` | `1` | Initial broker reconnect delay in seconds. |
| `reconnect_max_delay` | `30` | Maximum broker reconnect delay in seconds. |

## Basic (no TLS)

```json
{
  "network_protocol": {
    "module": "hivemind-mqtt-plugin",
    "hivemind-mqtt-plugin": {
      "broker_host": "192.168.1.100",
      "broker_port": 1883
    }
  }
}
```

Certificate verification remains enabled by default. For a private broker CA,
mount only the CA certificate into the listener and set `tls_ca_certs` to that
read-only path.

## With broker authentication

```json
{
  "network_protocol": {
    "module": "hivemind-mqtt-plugin",
    "hivemind-mqtt-plugin": {
      "broker_host": "192.168.1.100",
      "broker_port": 1883,
      "broker_username": "hivemind-hub",
      "broker_password": "secret"
    }
  }
}
```

## With TLS

```json
{
  "network_protocol": {
    "module": "hivemind-mqtt-plugin",
    "hivemind-mqtt-plugin": {
      "broker_host": "mqtt.example.com",
      "broker_port": 8883,
      "tls": true,
      "tls_ca_certs": "/etc/ssl/certs/ca-certificates.crt"
    }
  }
}
```

## With mTLS (client certificate)

```json
{
  "network_protocol": {
    "module": "hivemind-mqtt-plugin",
    "hivemind-mqtt-plugin": {
      "broker_host": "mqtt.example.com",
      "broker_port": 8883,
      "tls": true,
      "tls_ca_certs": "/path/to/ca.crt",
      "tls_certfile": "/path/to/hub.crt",
      "tls_keyfile": "/path/to/hub.key"
    }
  }
}
```
