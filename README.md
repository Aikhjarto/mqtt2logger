# mqtt2logger

Forwards the logs that ESPHome and Tasmota devices publish via MQTT to
rsyslog and systemd-journald, each configurable. The severity and the time
stamp are taken from the messages where they contain them.

MQTT instead of the devices' UDP syslog has two advantages: TCP repeats what
WiFi loses, and with QoS 1 and a persistent session the broker keeps the
messages while mqtt2logger or its machine is down. Neither helps while a
device has no network at all; Tasmota at least keeps a small buffer and
sends it once it is connected again.

## What it does

| | ESPHome | Tasmota |
|---|---|---|
| Topic | `+/debug` (`logger:` with `mqtt: log_topic:`) | `stat/+/LOGGING` (`MqttLog 1`..`4`) |
| Payload | `[W][modbus_controller:083]: Write not sent`, in ANSI colours | `13:45:21.123-029 MQT: Connected` |
| Severity | from the letter: E err, W warning, I and C info, D, V and VV debug | not in the payload: `tasmota: priority` (default info), or a priority rule |
| Time stamp | not in the payload: the time of reception | the device's local time of day, if plausible, else the time of reception |
| Device | the topic level at the `+` | the topic level at the `+` |

To rsyslog, each line goes as RFC 5424, by default over TCP to
`127.0.0.1:10514` with octet counting: the device as HOSTNAME, `esphome` or
`tasmota` as APP-NAME, the component or tag as MSGID, facility `local0`.

```
<132>1 2026-10-05T09:52:26.866618+02:00 heizungesp esphome - modbus - Stop waiting for response from 7
```

To journald, it uses the native protocol, with `SYSLOG_IDENTIFIER` the
device and the fields `DEVICE`, `DEVICE_FIRMWARE`, `DEVICE_COMPONENT`,
`MQTT_TOPIC`, and `DEVICE_TIMESTAMP` (microseconds) when the time stamp came
from the device: journald stamps entries with its own time and the host's
name, so the device's time and name are fields.

If journald forwards to rsyslog (`ForwardToSyslog=yes`, or rsyslog's
imjournal), enabling both outputs gives rsyslog each message twice, the
second time with the local host name. Then forward to rsyslog only, or to
journald only.

## Requirements

- Python 3.7 or newer, [paho-mqtt](https://pypi.org/project/paho-mqtt/) 1.5
  or newer, PyYAML for a configuration file
- for rsyslog: an input, see below

## Usage

```sh
mqtt2logger -H mqtt.example.org                       # to rsyslog at tcp://127.0.0.1:10514
mqtt2logger -H mqtt.example.org --journal --no-rsyslog
mqtt2logger --config /etc/mqtt2logger.yaml -v         # logs each forwarded message
```

| Option | Meaning |
|---|---|
| `-C`, `--config` | YAML configuration file, see `mqtt2logger.yaml.example` |
| `-H`, `-p` | MQTT broker and port (default: localhost, 1883 or 8883 with TLS) |
| `-u`, `--mqtt-password-file` | User and a file holding the password (also `$MQTT_PASSWORD_FILE`) |
| `--tls`, `--ca-cert` | TLS to the broker |
| `--client-id` | Client id; keep it fixed for the persistent session (default: `mqtt2logger-HOSTNAME`) |
| `--esphome-topic`, `--tasmota-topic` | Log topics, `+` marking the device; repeatable |
| `--rsyslog ADDRESS` | `tcp://HOST:PORT`, `udp://HOST:PORT` or `unix:PATH` |
| `--no-rsyslog`, `--journal`, `--no-journal` | Choose the outputs |
| `-v` | Log each forwarded message |

## Reliability

- **Broker**: mqtt2logger subscribes with QoS 1 and `clean_session: false`
  under a fixed client id, so the broker keeps the QoS 1 messages that arrive
  while it is disconnected. That needs the devices to publish with QoS 1:
  ESPHome's `log_topic` takes `qos: 1`, see below. mosquitto keeps QoS 0
  messages for a disconnected client only with `queue_qos0_messages true`.
- **rsyslog and journald**: every output has its own queue
  (`queue_size`, default 10000 messages). While rsyslog or journald is
  unavailable, messages wait and are sent once it is back; when the queue is
  full, the oldest are dropped and that is logged. On SIGTERM the queues are
  delivered for up to 10 seconds.
- **Retained messages** are skipped (`ignore_retained`): ESPHome retains its
  last log message by default, which would be logged again on every start.

### Device settings

ESPHome:

```yaml
mqtt:
  broker: mqtt.example.org
  log_topic:
    topic: heizungesp/debug
    qos: 1
    retain: false
    level: INFO        # what goes to MQTT; the logger's level is the upper bound
```

Tasmota: `MqttLog 2` sends errors and info messages. Tasmota has no setting
for the QoS of its log messages; if it publishes them with QoS 0, which I have
not verified, the broker keeps them for a disconnected mqtt2logger only with
mosquitto's `queue_qos0_messages true`.

## rsyslog

`data/rsyslog-mqtt2logger.conf` adds the TCP input on `127.0.0.1:10514` and
writes one file per device plus `/var/log/devices/warn` with the warnings and
errors of all of them:

```sh
install -m 0644 data/rsyslog-mqtt2logger.conf /etc/rsyslog.d/mqtt2logger.conf
systemctl restart rsyslog
```

log_digest reads `/var/log/devices/warn` as warnings and worse, from
rsyslog's configuration.

## Severity of Tasmota messages

Tasmota does not publish a line's severity. `tasmota: priority` gives all of
them one (default info). With `MqttLog 1` a device sends only errors: give
its source `priority: err`. Priority rules set the severity by device,
firmware, tag, message or topic; the first matching rule wins:

```yaml
priority_rules:
  - {firmware: tasmota, message: '(?i)\b(error|fail(ed|ure)?|timeout)\b', priority: err}
```

## Time stamps of Tasmota messages

Tasmota prefixes each line with its local time of day, without a date.
mqtt2logger takes the date that puts it closest to the time of reception,
and uses it if it is at most `timestamps: max_age` seconds old (default 3600)
and at most `max_future` seconds ahead (default 120); otherwise, as when the
device's clock is not synchronized yet, the time of reception. The device has
to be in the time zone of the host running mqtt2logger.

## systemd

```sh
install -m 0755 mqtt2logger /usr/bin/
install -m 0644 mqtt2logger.yaml.example /etc/mqtt2logger.yaml
install -m 0644 data/mqtt2logger.sysusers /usr/lib/sysusers.d/mqtt2logger.conf
install -m 0644 data/mqtt2logger.service /etc/systemd/system/
systemd-sysusers
systemctl enable --now mqtt2logger
```

The service runs as the user `mqtt2logger`; a password file must be readable
by it.

## Bash completion

`completions/mqtt2logger` completes the options and their values. The packages
install it; otherwise copy it to bash-completion's directory:

```sh
install -D -m 0644 completions/mqtt2logger /usr/share/bash-completion/completions/mqtt2logger
```

## Tests

```sh
python3 -m unittest -v
```

The tests run mosquitto on a free port, a fake rsyslog TCP server and a fake
journald socket, and rsyslogd itself if installed. They skip the daemon tests
without mosquitto or paho-mqtt. `tests/test_completion.py` checks the bash
completion against `--help`.

## License

GPL-2.0-or-later, see [LICENSE](LICENSE).
