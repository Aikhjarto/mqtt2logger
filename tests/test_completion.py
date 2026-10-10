"""Tests of the bash completion in completions/mqtt2logger."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON = os.path.join(ROOT, "mqtt2logger")
COMPLETION = os.path.join(ROOT, "completions", "mqtt2logger")


def complete(*words, cwd=None):
    """The candidates bash offers for the last of words, which may be empty."""
    words = ("mqtt2logger",) + words
    script = 'source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD=$(($# - 1)); ' \
             '$(complete -p "$1" | sed -E "s/.*-F ([^ ]+) .*/\\1/"); printf "%s\\n" "${COMPREPLY[@]}"'
    result = subprocess.run(["bash", "-c", script, "bash", COMPLETION] + list(words),
                            capture_output=True, text=True, check=True, cwd=cwd)
    return [line for line in result.stdout.split("\n") if line]


def help_options():
    """The option strings --help lists.

    Before Python 3.13 argparse repeats the metavar after every option string
    ("-H HOST, --host HOST"), since then only after the last ("-H, --host HOST").
    An option line starts with the invocation, followed by the description after
    two or more spaces or, if the invocation is long, on the next line.
    """
    text = subprocess.run([sys.executable, DAEMON, "--help"], capture_output=True, text=True, check=True).stdout
    options = set()
    for line in text.split("\n"):
        match = re.match(r"  (-\S.*?)(?:\s{2,}|$)", line)
        if match:
            options.update(re.findall(r"(?:^|, )(-{1,2}[^\s,=\[]+)", match.group(1)))
    return options


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class CompletionTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", COMPLETION], check=True)

    def test_offers_exactly_the_options_of_help(self):
        self.assertEqual(set(complete("")), help_options())

    def test_option_prefix(self):
        self.assertEqual(complete("--no-"), ["--no-rsyslog", "--no-journal"])
        self.assertEqual(complete("--tas"), ["--tasmota-topic"])

    def test_files(self):
        with tempfile.TemporaryDirectory() as directory:
            open(os.path.join(directory, "mqtt.password"), "w").close()
            for option in ("-C", "--config", "--mqtt-password-file", "--ca-cert"):
                self.assertEqual(complete(option, "mq", cwd=directory), ["mqtt.password"], option)
            self.assertEqual(complete("--mqtt-password-file", "=", "mq", cwd=directory), ["mqtt.password"])

    def test_no_candidates_for_values(self):
        for option in ("-p", "--mqtt-username", "--client-id", "--esphome-topic", "--tasmota-topic",
                       "--rsyslog"):
            self.assertEqual(complete(option, ""), [], option)
            self.assertEqual(complete(option, "=", ""), [], option)


if __name__ == "__main__":
    unittest.main()
