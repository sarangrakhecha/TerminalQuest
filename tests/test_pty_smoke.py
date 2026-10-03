"""Smoke tests that run the REAL game in a pseudo-terminal.

Everything else in the suite drives the game through a fake screen. These
launch `terminalquest.py` exactly as a player would — a real process on a real
(pseudo) tty, real curses, real escape sequences — using only the standard
library (`pty`, `termios`), so they run anywhere the game does.
"""
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.path.join(ROOT, "terminalquest.py")
UP, DOWN, RIGHT, LEFT = "\x1bOA", "\x1bOB", "\x1bOC", "\x1bOD"   # keypad mode, as curses sets it


class Session:
    """A running game on a pty: send keys, wait for text, check how it exits."""

    def __init__(self, tmp_path, rows=40, cols=132, extra_args=()):
        env = dict(os.environ, TERM="xterm-256color", TERMINALQUEST_ROOT=str(tmp_path / "arcade"))
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe(sys.executable, [sys.executable, GAME, "--reset", *extra_args], env)
        self.resize(rows, cols, signal_child=False)
        self.out = ""
        self.status = None

    def resize(self, rows, cols, signal_child=True):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if signal_child:
            os.kill(self.pid, signal.SIGWINCH)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, end - time.monotonic()))
            if not ready:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.out += data.decode("utf-8", "replace")

    def wait_for(self, text, timeout=10):
        """Read until `text` has appeared in the output (anywhere since the last call)."""
        end = time.monotonic() + timeout
        start = len(self.out)
        while time.monotonic() < end:
            if text in self.out[start:]:
                return True
            self.pump(0.1)
        return text in self.out[start:]

    def send(self, data):
        os.write(self.fd, data.encode())

    def running(self):
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = status
        return self.status is None

    def exit_code(self, timeout=10):
        end = time.monotonic() + timeout
        while self.running() and time.monotonic() < end:
            self.pump(0.1)
        if self.status is None:
            return None
        self.pump(0.2)
        return os.waitstatus_to_exitcode(self.status) if hasattr(os, "waitstatus_to_exitcode") else (
            os.WEXITSTATUS(self.status) if os.WIFEXITED(self.status) else -1)

    def close(self):
        if self.running():
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self.fd)


@pytest.fixture
def session(tmp_path):
    made = []

    def start(**kw):
        s = Session(tmp_path, **kw)
        made.append(s)
        return s

    yield start
    for s in made:
        s.close()


def start_playing(s):
    assert s.wait_for("press any key to start"), s.out[-300:]
    s.send(" ")
    assert s.wait_for("next: terminal"), s.out[-300:]


def test_the_intro_stays_up_until_a_real_key(session):
    s = session()
    assert s.wait_for("press any key to start")
    s.pump(1.0)                               # long enough for an idle tick to (wrongly) dismiss it
    assert s.running() and "next: terminal" not in s.out
    s.send(" ")
    assert s.wait_for("next: terminal")


def test_a_full_start_and_quit_exits_cleanly_and_prints_the_cheat_sheet(session):
    s = session()
    start_playing(s)
    s.send("q")
    assert s.wait_for("QUIT TERMINALQUEST?")
    s.send("y")
    assert s.exit_code() == 0
    s.pump(0.3)
    assert "cheat sheet" in s.out and "Traceback" not in s.out


def test_n_at_the_quit_prompt_keeps_playing(session):
    s = session()
    start_playing(s)
    s.send("q")
    assert s.wait_for("QUIT TERMINALQUEST?")
    s.send("n")
    s.pump(0.5)
    assert s.running()


def test_arrow_keys_and_toggles_do_not_disturb_the_game(session):
    s = session()
    start_playing(s)
    for key in (RIGHT, DOWN, LEFT, UP, "e", "e", "b", "b", "t", "t"):
        s.send(key)
        s.pump(0.05)
    s.pump(0.4)
    assert s.running() and "Traceback" not in s.out


def test_shrinking_the_window_shows_a_message_and_growing_it_brings_the_game_back(session):
    s = session()
    start_playing(s)
    s.resize(10, 40)
    assert s.wait_for("Please resize")
    assert s.running()
    s.out = ""
    s.resize(40, 132)
    assert s.wait_for("next: terminal")
    assert s.running() and "Traceback" not in s.out


def test_resizing_during_the_intro_does_not_start_the_game(session):
    s = session()
    assert s.wait_for("press any key to start")
    s.resize(36, 120)
    s.pump(0.8)
    assert s.running() and "next: terminal" not in s.out
    s.send(" ")
    assert s.wait_for("next: terminal")


def test_ctrl_c_exits_without_a_traceback(session):
    s = session()
    start_playing(s)
    s.send("\x03")
    assert s.exit_code() is not None
    assert "Traceback" not in s.out and "KeyboardInterrupt" not in s.out


def test_the_game_idles_quietly(session):
    s = session()
    start_playing(s)
    s.pump(1.0)                                # let the first frames settle
    before = len(s.out)
    s.pump(3.0)
    # 30 frames in 3s; ncurses only repaints what changed, so output stays small
    assert len(s.out) - before < 200_000
    assert s.running()


def test_holding_a_key_does_not_flood_the_game(session):
    s = session()
    start_playing(s)
    for _ in range(300):                       # ~ a held arrow key for several seconds
        s.send(RIGHT if _ % 40 < 20 else LEFT)
        s.pump(0.003)
    s.pump(0.5)
    assert s.running() and "Traceback" not in s.out


def test_no_enemies_flag_is_accepted(session):
    s = session(extra_args=("--no-enemies",))
    start_playing(s)
    s.send("q")
    s.wait_for("QUIT TERMINALQUEST?")
    s.send("y")
    assert s.exit_code() == 0
