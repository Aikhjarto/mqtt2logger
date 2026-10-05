"""Tests for mqtt2logger: the parsers and output formats, and the daemon against a local mosquitto,
a fake rsyslog TCP server, a fake journald socket and, if installed, a real rsyslogd."""
import datetime
import importlib.machinery
import importlib.util
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(os.path.dirname(HERE), "mqtt2logger")

loader = importlib.machinery.SourceFileLoader("mqtt2logger", DAEMON)
spec = importlib.util.spec_from_loader("mqtt2logger", loader)
m2l = importlib.util.module_from_spec(spec)
loader.exec_module(m2l)

try:
    import paho.mqtt.client as paho
    import paho.mqtt.publish as publish
except ImportError:
    paho = None

MOSQUITTO = shutil.which("mosquitto") or next(
    (path for path in ("/usr/sbin/mosquitto", "/usr/bin/mosquitto") if os.path.exists(path)), None)
RSYSLOGD = shutil.which("rsyslogd") or next(
    (path for path in ("/usr/sbin/rsyslogd", "/sbin/rsyslogd") if os.path.exists(path)), None)
TIMESTAMPS = {"max_age": 3600, "max_future": 120}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clock(seconds_ago, heap=""):
    return time.strftime("%H:%M:%S", time.localtime(time.time() - seconds_ago)) + ".123" + heap


def read(path):
    with open(path) as handle:
        return handle.read()


def decode_journal(data):
    fields = {}
    index = 0
    while index < len(data):
        end = data.index(b"\n", index)
        line = data[index:end]
        if b"=" in line:
            key, value = line.split(b"=", 1)
            index = end + 1
        else:
            length = struct.unpack("<Q", data[end + 1:end + 9])[0]
            key, value = line, data[end + 9:end + 9 + length]
            index = end + 9 + length + 1
        fields[key.decode()] = value.decode()
    return fields


class TestParsers(unittest.TestCase):
    def test_esphome(self):
        lines = m2l.parse_esphome("\x1b[0;33m[W][modbus_controller:083]: Write not sent: register 8\x1b[0m", 100.0)
        self.assertEqual([(4, "modbus_controller", "Write not sent: register 8", 100.0, False)], lines)
        for letter, level in (("E", 3), ("I", 6), ("C", 6), ("D", 7), ("V", 7), ("VV", 7)):
            self.assertEqual(level, m2l.parse_esphome("[%s][tag]: x" % letter, 1.0)[0][0], letter)
        lines = m2l.parse_esphome("[C][wifi:443]: WiFi:\n  Local MAC: AA\n  SSID: x", 1.0)
        self.assertEqual(["WiFi:", "  Local MAC: AA", "  SSID: x"], [line[2] for line in lines])
        self.assertEqual({6}, {line[0] for line in lines})
        self.assertEqual([(None, "", "no prefix", 1.0, False)], m2l.parse_esphome("no prefix", 1.0))

    def test_tasmota(self):
        now = time.time()
        (priority, tag, message, stamp, from_device), = m2l.parse_tasmota(
            "%s MQT: tele/tasmota/STATE = {}" % clock(5), now, TIMESTAMPS)
        self.assertIsNone(priority)
        self.assertEqual(("MQT", "tele/tasmota/STATE = {}"), (tag, message))
        self.assertTrue(from_device)
        self.assertAlmostEqual(now - 5, stamp, delta=1.2)
        # free heap and fragmentation after the time stamp
        (_, tag, message, stamp, from_device), = m2l.parse_tasmota("%s HTP: Web server active" % clock(30, "-029/12"),
                                                                    now, TIMESTAMPS)
        self.assertEqual(("HTP", "Web server active"), (tag, message))
        self.assertAlmostEqual(now - 30, stamp, delta=1.2)
        # a line without a tag
        (_, tag, message, _, _), = m2l.parse_tasmota("%s plain text" % clock(1), now, TIMESTAMPS)
        self.assertEqual(("", "plain text"), (tag, message))

    def test_tasmota_implausible_time(self):
        # an unsynchronized clock counts from midnight: far from now, so the reception time is used
        now = datetime.datetime.now().replace(hour=13, minute=0, second=0).timestamp()
        (_, _, _, stamp, from_device), = m2l.parse_tasmota("00:00:07.512 APP: Boot Count 31", now, TIMESTAMPS)
        self.assertEqual((now, False), (stamp, from_device))

    def test_tasmota_across_midnight(self):
        now = datetime.datetime.now().replace(hour=0, minute=0, second=30, microsecond=0).timestamp()
        (_, _, _, stamp, from_device), = m2l.parse_tasmota("23:59:50.000 MQT: late", now, TIMESTAMPS)
        self.assertTrue(from_device)
        self.assertAlmostEqual(now - 40, stamp, delta=0.01)

    def test_source(self):
        source = m2l.Source({"type": "tasmota", "topic": "stat/+/LOGGING"})
        self.assertTrue(source.matches("stat/kitchen/LOGGING"))
        self.assertFalse(source.matches("stat/kitchen/RESULT"))
        self.assertEqual("kitchen", source.device_of("stat/kitchen/LOGGING"))
        source = m2l.Source({"type": "esphome", "topic": "esp/#", "device_level": 1})
        self.assertEqual("pump", source.device_of("esp/pump/debug"))
        with self.assertRaises(m2l.ConfigError):
            m2l.Source({"type": "shelly", "topic": "x"})

    def test_rules(self):
        config = m2l.merge_config(m2l.DEFAULTS, {"priority_rules": [
            {"firmware": "tasmota", "message": "(?i)error|fail", "priority": "err"},
            {"device": "^noisy$", "priority": "debug"}], "journal": {"enabled": True}})
        forwarder = m2l.Forwarder(config)
        records = forwarder.records("stat/plug/LOGGING", ("%s MQT: Connect failed" % clock(1)).encode(), time.time())
        self.assertEqual(3, records[0].priority)
        records = forwarder.records("stat/plug/LOGGING", ("%s MQT: Connected" % clock(1)).encode(), time.time())
        self.assertEqual(6, records[0].priority, "Tasmota's default")
        records = forwarder.records("noisy/debug", b"[W][x]: y", time.time())
        self.assertEqual(7, records[0].priority)
        self.assertEqual([], forwarder.records("other/topic", b"x", time.time()))

    def test_rfc5424(self):
        record = m2l.Record("heizungesp", "esphome", "modbus_controller", "Write not sent", 4, 1790000000.25,
                            False, "heizungesp/debug")
        line = m2l.rfc5424(record, 16, "heizungesp", "esphome")
        stamp = datetime.datetime.fromtimestamp(1790000000.25).astimezone().isoformat(timespec="microseconds")
        self.assertEqual("<132>1 %s heizungesp esphome - modbus_controller - Write not sent" % stamp, line)
        record.tag = "with space"
        record.message = "two\nlines"
        self.assertTrue(m2l.rfc5424(record, 16, "a b", "").endswith(" ab - - withspace - two lines"))

    def test_journal_datagram(self):
        record = m2l.Record("plug", "tasmota", "MQT", "line one\nline two", 3, 1790000000.0, True, "stat/plug/LOGGING")
        fields = decode_journal(m2l.journal_datagram(m2l.journal_fields(record, "plug", 16)))
        self.assertEqual("line one\nline two", fields["MESSAGE"])
        self.assertEqual("3", fields["PRIORITY"])
        self.assertEqual("plug", fields["SYSLOG_IDENTIFIER"])
        self.assertEqual("MQT", fields["DEVICE_COMPONENT"])
        self.assertEqual("1790000000000000", fields["DEVICE_TIMESTAMP"])


class FakeSyslogTCP:
    """Receives RFC 6587 octet counted syslog messages."""

    def __init__(self, port=0):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", port))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.messages = []
        self.running = True
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while self.running:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.read, args=(conn,), daemon=True).start()

    def read(self, conn):
        buffer = b""
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                break
            if not data:
                break
            buffer += data
            while b" " in buffer:
                length, rest = buffer.split(b" ", 1)
                if len(rest) < int(length):
                    break
                self.messages.append(rest[:int(length)].decode())
                buffer = rest[int(length):]
        conn.close()

    def close(self):
        self.running = False
        self.server.close()


class FakeJournal:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(path)
        self.entries = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                self.entries.append(decode_journal(self.sock.recv(1 << 20)))
            except OSError:
                return

    def close(self):
        self.sock.close()


def wait_for(condition, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


@unittest.skipUnless(MOSQUITTO and paho, "mosquitto or paho-mqtt is not installed")
class TestDaemon(unittest.TestCase):
    def setUp(self):
        # a short path: Unix socket paths are limited to 108 bytes
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.broker_port = free_port()
        with open(os.path.join(self.dir, "mosquitto.conf"), "w") as handle:
            handle.write("listener %d 127.0.0.1\nallow_anonymous true\n" % self.broker_port)
        self.broker = subprocess.Popen([MOSQUITTO, "-c", os.path.join(self.dir, "mosquitto.conf")],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(wait_for(self.broker_up, 10), "mosquitto did not start")
        self.syslog = FakeSyslogTCP()
        self.journal = FakeJournal(os.path.join(self.dir, "journal.sock"))
        self.daemons = []

    def broker_up(self):
        try:
            socket.create_connection(("127.0.0.1", self.broker_port), timeout=1).close()
            return True
        except OSError:
            return False

    def tearDown(self):
        for daemon in self.daemons:
            if daemon.poll() is None:
                daemon.kill()
                daemon.wait()
        self.broker.terminate()
        self.broker.wait()
        self.syslog.close()
        self.journal.close()
        shutil.rmtree(self.dir)

    def start(self, *extra, client_id="m2l-test", journal=True):
        config = os.path.join(self.dir, "m2l.yaml")
        with open(config, "w") as handle:
            handle.write("journal: {enabled: %s, socket: %s}\n" % (str(journal).lower(),
                                                                   os.path.join(self.dir, "journal.sock")))
        log_path = os.path.join(self.dir, "daemon-%d.log" % len(self.daemons))
        log_file = open(log_path, "w")
        self.addCleanup(log_file.close)
        daemon = subprocess.Popen([sys.executable, DAEMON, "-C", config, "-H", "127.0.0.1", "-p", str(self.broker_port),
                                   "--client-id", client_id, "--rsyslog", "tcp://127.0.0.1:%d" % self.syslog.port]
                                  + list(extra), stdout=subprocess.DEVNULL, stderr=log_file)
        self.daemons.append(daemon)
        self.assertTrue(wait_for(lambda: "subscribed to stat/+/LOGGING" in read(log_path), 20),
                        read(log_path))
        return daemon, log_path

    def publish(self, topic, payload, qos=1, retain=False):
        publish.single(topic, payload, qos=qos, retain=retain, hostname="127.0.0.1", port=self.broker_port)

    def stop(self, daemon):
        daemon.terminate()
        self.assertEqual(0, daemon.wait(20))

    def test_forwarding(self):
        daemon, _ = self.start()
        self.publish("heizungesp/debug", "\x1b[0;33m[W][modbus_controller:083]: Write not sent\x1b[0m")
        self.publish("stat/plug/LOGGING", "%s MQT: Connected" % clock(3))
        self.assertTrue(wait_for(lambda: len(self.syslog.messages) >= 2 and len(self.journal.entries) >= 2))
        esphome = next(message for message in self.syslog.messages if "Write not sent" in message)
        self.assertRegex(esphome, r"^<132>1 \S+ heizungesp esphome - modbus_controller - Write not sent$")
        tasmota = next(message for message in self.syslog.messages if "Connected" in message)
        self.assertRegex(tasmota, r"^<134>1 \S+ plug tasmota - MQT - Connected$")
        sent = datetime.datetime.fromisoformat(tasmota.split()[1]).timestamp()
        self.assertAlmostEqual(time.time() - 3, sent, delta=2, msg="Tasmota's own time stamp")
        entry = next(entry for entry in self.journal.entries if entry["DEVICE"] == "plug")
        self.assertEqual("6", entry["PRIORITY"])
        self.assertIn("DEVICE_TIMESTAMP", entry)
        self.stop(daemon)

    def test_retained_messages_are_skipped(self):
        self.publish("stale/debug", "[E][old]: retained before we subscribed", retain=True)
        daemon, _ = self.start()
        self.publish("fresh/debug", "[I][new]: current")
        self.assertTrue(wait_for(lambda: any("current" in message for message in self.syslog.messages)))
        self.assertFalse(any("retained" in message for message in self.syslog.messages))
        self.stop(daemon)

    def test_persistent_session(self):
        # QoS 1 messages published while the daemon is down are delivered when it is back
        daemon, _ = self.start(client_id="m2l-persistent", journal=False)
        self.stop(daemon)
        self.publish("away/debug", "[E][x]: published while the daemon was down", qos=1)
        daemon, log_path = self.start(client_id="m2l-persistent", journal=False)
        self.assertTrue(wait_for(lambda: any("while the daemon was down" in message
                                             for message in self.syslog.messages)), self.syslog.messages)
        self.assertIn("session resumed", read(log_path))
        self.stop(daemon)

    def test_rsyslog_down_then_up(self):
        port = free_port()
        daemon, log_path = self.start("--rsyslog", "tcp://127.0.0.1:%d" % port, journal=False)
        self.publish("late/debug", "[W][x]: waits for rsyslog")
        self.assertTrue(wait_for(lambda: "rsyslog:" in read(log_path) and "retrying" in read(log_path)))
        late = FakeSyslogTCP(port)
        try:
            self.assertTrue(wait_for(lambda: any("waits for rsyslog" in message for message in late.messages), 30))
        finally:
            late.close()
        self.stop(daemon)

    def test_shutdown_delivers_the_queue(self):
        daemon, log_path = self.start(journal=False)
        for number in range(200):
            self.publish("burst/debug", "[D][x]: message %d" % number, qos=0)
        self.assertTrue(wait_for(lambda: len(self.syslog.messages) >= 200), len(self.syslog.messages))
        self.stop(daemon)
        self.assertIn("rsyslog: 200 message(s) sent, 0 dropped, 0 left", read(log_path))

    @unittest.skipUnless(RSYSLOGD, "rsyslogd is not installed")
    def test_real_rsyslogd(self):
        port = free_port()
        out = os.path.join(self.dir, "rsyslog.out")
        conf = os.path.join(self.dir, "rsyslog.conf")
        with open(conf, "w") as handle:
            handle.write('global(workDirectory="%s")\nmodule(load="imtcp")\n'
                         'input(type="imtcp" port="%d" address="127.0.0.1")\n'
                         'template(name="t" type="string" string="%%syslogseverity-text%% %%timereported:::date-rfc3339%% '
                         '%%hostname%% %%app-name%% %%msgid%% %%msg%%\\n")\n'
                         '*.* action(type="omfile" file="%s" template="t")\n' % (self.dir, port, out))
        rsyslogd = subprocess.Popen([RSYSLOGD, "-n", "-f", conf, "-i", os.path.join(self.dir, "rsyslog.pid")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_for(lambda: self.port_open(port), 10), "rsyslogd did not start")
            daemon, _ = self.start("--rsyslog", "tcp://127.0.0.1:%d" % port, journal=False)
            self.publish("stat/plug/LOGGING", "%s WIF: Connect failed" % clock(4))
            self.publish("pump/debug", "[E][sensor:12]: bad reading")
            self.assertTrue(wait_for(lambda: os.path.exists(out) and len(read(out).splitlines()) >= 2, 20))
            lines = read(out).splitlines()
            self.stop(daemon)
        finally:
            rsyslogd.terminate()
            rsyslogd.wait()
        tasmota = next(line for line in lines if "Connect failed" in line).split(" ", 5)
        self.assertEqual(["info", "plug", "tasmota", "WIF", "Connect failed"], [tasmota[0]] + tasmota[2:])
        self.assertAlmostEqual(time.time() - 4, datetime.datetime.fromisoformat(tasmota[1]).timestamp(), delta=10)
        esphome = next(line for line in lines if "bad reading" in line).split(" ", 5)
        self.assertEqual(["err", "pump", "esphome", "sensor", "bad reading"], [esphome[0]] + esphome[2:])

    def port_open(self, port):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            return False


class TestCommandLine(unittest.TestCase):
    def test_configuration_errors(self):
        result = subprocess.run([sys.executable, DAEMON, "--no-rsyslog"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        self.assertEqual(2, result.returncode)
        self.assertIn("neither rsyslog nor journal output is enabled", result.stderr)
        result = subprocess.run([sys.executable, DAEMON, "--rsyslog", "smtp://x"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        self.assertEqual(2, result.returncode)
        self.assertIn("rsyslog address is", result.stderr)

    def test_version(self):
        result = subprocess.run([sys.executable, DAEMON, "--version"], stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual("mqtt2logger %s\n" % m2l.VERSION, result.stdout)


if __name__ == "__main__":
    unittest.main()
