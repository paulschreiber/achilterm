#!/usr/bin/env python

"""Achilterm is an AJAX web based terminal, very easy to install and to use.

Copyright (C) 2013-2016 Florent Gallaire <fgallaire@gmail.com>
Copyright (C) 2006 Antony Lesuisse (email: al AT udev.org)

Modified in November 2026 by Paul Schreiber and published as achilterm-ng:
https://github.com/paulschreiber/achilterm

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import array
import codecs
import contextlib
import fcntl
import glob
import gzip
import html
import logging
import mimetypes
import os
import pty
import re
import secrets
import selectors
import signal
import struct
import sys
import termios
import threading
import time
from importlib import metadata
from socket import gethostname
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import webob
from webob.request import DisconnectionError

try:
    __version__ = metadata.version("achilterm-ng")
except metadata.PackageNotFoundError:
    # Running from a source checkout without installing.
    __version__ = "unknown"

logger = logging.getLogger(__name__)


# A screen cell holds a character in its low 24 bits, which is enough for any
# Unicode character, below its colors: 0xBBFF, a byte each for background and
# foreground.
CHAR_BITS = 24
DEFAULT_SGR = 0x0007
BLANK = DEFAULT_SGR << CHAR_BITS | ord(" ")

# Characters that aren't shown: controls, some of which would make the XML sent
# to the browser invalid, and those that would reorder or break screen lines.
UNPRINTABLE = frozenset(
    map(
        chr,
        [
            *range(0x20),
            *range(0x7F, 0xA0),
            0x061C,
            0x200E,
            0x200F,
            *range(0x2028, 0x202F),
            *range(0x2066, 0x206A),
            0xFFFE,
            0xFFFF,
        ],
    )
)


# Close a terminal when its browser hasn't asked for updates for this many
# seconds. Browsers may slow a hidden tab's requests to one a minute, so shorter
# timeouts than the minimum would close terminals that are still open.
DEFAULT_TIMEOUT = 300
MINIMUM_TIMEOUT = 90
# The TCP ports the server can be given; 0 picks a free one.
PORTS = range(65536)
# Kill what's left of a closed terminal's command after this many seconds.
KILL_DELAY = 5
# The sizes a terminal can have, and the usual one.
WIDTHS = range(3, 256)
HEIGHTS = range(3, 100)
DEFAULT_WIDTH, DEFAULT_HEIGHT = 80, 25
# Signals that stop the server.
STOP_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
# Names a request may use for this server. Refusing others stops a website from
# reaching it by pointing its own name at this machine.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def host_name(host):
    """The name in a Host header, without any port, or None if it isn't one."""
    try:
        return urlsplit("//" + host).hostname
    except ValueError:
        return None


def allowed_host(value):
    """A name given with --allow-host, in the form host_name() returns."""
    host = value
    if host.count(":") > 1 and not host.startswith("["):
        host = f"[{host}]"  # an IPv6 address given without brackets
    name = host_name(host)
    if (
        not name
        or not re.fullmatch("[a-z0-9._:-]+", name)
        or any(character in host for character in "/@?#")
    ):
        raise ValueError(f"{value!r} isn't a host name")
    return name


def refuse(status, message, **headers):
    response = webob.Response(text=message, status=status, content_type="text/plain")
    response.headers.update(headers)
    return response


def clamp(value, low, high):
    return max(low, min(value, high))


def whole_number(value, name, allowed):
    """A width or height as a number, if it is one of those allowed."""
    with contextlib.suppress(ValueError):
        # In decimal, with any leading zeros, and without cutting off fractions.
        number = int(str(value))
        if number in allowed:
            return number
    smallest, largest = allowed[0], allowed[-1]
    raise ValueError(f"{name} must be a whole number from {smallest} to {largest}")


def requested_size(fields):
    """The size a page asks for a new terminal, or the usual size if that's
    missing or no terminal can have it."""
    try:
        return (
            whole_number(fields.get("w"), "w", WIDTHS),
            whole_number(fields.get("h"), "h", HEIGHTS),
        )
    except ValueError:
        return DEFAULT_WIDTH, DEFAULT_HEIGHT


class Terminal:
    def __init__(self, width=80, height=24):
        self.width = width
        self.height = height
        # Output arrives in arbitrary chunks, which can split a character, and
        # programs may write bytes that aren't UTF-8.
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.init()
        self.reset()

    def init(self):
        self.esc_seq = {
            "\x05": self.esc_da,
            "\x08": self.esc_0x08,
            "\x09": self.esc_0x09,
            "\x0a": self.esc_0x0a,
            "\x0b": self.esc_0x0a,
            "\x0c": self.esc_0x0a,
            "\x0d": self.esc_0x0d,
            "\x1b#8": None,
            "\x1b=": None,
            "\x1b>": None,
            "\x1b(0": None,
            "\x1b(A": None,
            "\x1b(B": None,
            "\x1b[c": self.esc_da,
            "\x1b[0c": self.esc_da,
            "\x1b]R": None,
            "\x1b7": self.esc_save,
            "\x1b8": self.esc_restore,
            "\x1bD": None,
            "\x1bE": None,
            "\x1bH": None,
            "\x1bM": self.esc_ri,
            "\x1bN": None,
            "\x1bO": None,
            "\x1bZ": self.esc_da,
            "\x1ba": None,
            "\x1bc": self.reset,
            "\x1bn": None,
            "\x1bo": None,
        }
        for k, v in self.esc_seq.items():
            if v == None:
                self.esc_seq[k] = self.esc_ignore
        # regex
        d = {
            r"\[\??([0-9;]*)([@ABCDEFGHJKLMPXacdefghlmnqrstu`])": self.csi_dispatch,
            r"\]([^\x07]+)\x07": self.esc_ignore,
        }
        self.esc_re = []
        for k, v in d.items():
            self.esc_re.append((re.compile("\x1b" + k), v))
        # define csi sequences
        self.csi_seq = {
            "@": (self.csi_at, [1]),
            "`": (self.csi_G, [1]),
            "J": (self.csi_J, [0]),
            "K": (self.csi_K, [0]),
        }
        for i in [i[4] for i in dir(self) if i.startswith("csi_") and len(i) == 5]:
            if i not in self.csi_seq:
                self.csi_seq[i] = (getattr(self, "csi_" + i), [1])

    def reset(self, s=""):
        self.scr = array.array("q", [BLANK]) * (self.width * self.height)
        self.st = 0
        self.sb = self.height - 1
        self.cx_bak = self.cx = 0
        self.cy_bak = self.cy = 0
        self.cl = 0
        self.sgr = DEFAULT_SGR
        self.buf = ""
        self.outbuf = ""
        self.last_html = ""

    def peek(self, y1, x1, y2, x2):
        return self.scr[self.width * y1 + x1 : self.width * y2 + x2]

    def poke(self, y, x, s):
        pos = self.width * y + x
        self.scr[pos : pos + len(s)] = s

    def zero(self, y1, x1, y2, x2):
        w = self.width * (y2 - y1) + x2 - x1 + 1
        z = array.array("q", [BLANK]) * w
        self.scr[self.width * y1 + x1 : self.width * y2 + x2 + 1] = z

    # Each of these moves n cells or rows at once, where n is no more than there
    # are from the starting point to the end of the row or region.

    def scroll_up(self, y1, y2, n=1):
        self.poke(y1, 0, self.peek(y1 + n, 0, y2, self.width))
        self.zero(y2 - n + 1, 0, y2, self.width - 1)

    def scroll_down(self, y1, y2, n=1):
        self.poke(y1 + n, 0, self.peek(y1, 0, y2 - n, self.width))
        self.zero(y1, 0, y1 + n - 1, self.width - 1)

    def scroll_right(self, y, x, n):
        self.poke(y, x + n, self.peek(y, x, y, self.width - n))
        self.zero(y, x, y, x + n - 1)

    def cursor_down(self):
        if self.cy >= self.st and self.cy <= self.sb:
            self.cl = 0
            q, r = divmod(self.cy + 1, self.sb + 1)
            if q:
                self.scroll_up(self.st, self.sb)
                self.cy = self.sb
            else:
                self.cy = r

    def cursor_right(self):
        q, r = divmod(self.cx + 1, self.width)
        if q:
            self.cl = 1
        else:
            self.cx = r

    def echo(self, c):
        if self.cl:
            self.cursor_down()
            self.cx = 0
        self.scr[(self.cy * self.width) + self.cx] = self.sgr << CHAR_BITS | ord(c)
        self.cursor_right()

    def esc_0x08(self, s):
        self.cx = max(0, self.cx - 1)

    def esc_0x09(self, s):
        x = self.cx + 8
        q, _r = divmod(x, 8)
        self.cx = (q * 8) % self.width

    def esc_0x0a(self, s):
        self.cursor_down()

    def esc_0x0d(self, s):
        self.cl = 0
        self.cx = 0

    def esc_save(self, s):
        self.cx_bak = self.cx
        self.cy_bak = self.cy

    def esc_restore(self, s):
        self.cx = self.cx_bak
        self.cy = self.cy_bak
        self.cl = 0

    def esc_da(self, s):
        self.outbuf = "\x1b[?6c"

    def esc_ri(self, s):
        self.cy = max(self.st, self.cy - 1)
        if self.cy == self.st:
            self.scroll_down(self.st, self.sb)

    def esc_ignore(self, *s):
        pass

    # 		print "term:ignore: %s"%repr(s)
    def csi_dispatch(self, seq, mo):
        # CSI sequences
        s = mo.group(1)
        c = mo.group(2)
        f = self.csi_seq.get(c, None)
        if f:
            try:
                l = [min(int(i), 1024) for i in s.split(";") if len(i) < 4]
            except ValueError:
                l = []
            if len(l) == 0:
                l = f[1]
            f[0](l)

    # 		else:
    # 			print 'csi ignore',c,l
    def csi_at(self, l):
        self.scroll_right(self.cy, self.cx, min(l[0], self.width - self.cx))

    def csi_A(self, l):
        self.cy = max(self.st, self.cy - l[0])

    def csi_B(self, l):
        self.cy = min(self.sb, self.cy + l[0])

    def csi_C(self, l):
        self.cx = min(self.width - 1, self.cx + l[0])
        self.cl = 0

    def csi_D(self, l):
        self.cx = max(0, self.cx - l[0])
        self.cl = 0

    def csi_E(self, l):
        self.csi_B(l)
        self.cx = 0
        self.cl = 0

    def csi_F(self, l):
        self.csi_A(l)
        self.cx = 0
        self.cl = 0

    def csi_G(self, l):
        self.cx = clamp(l[0], 1, self.width) - 1

    def csi_H(self, l):
        if len(l) < 2:
            l = [1, 1]
        self.cx = clamp(l[1], 1, self.width) - 1
        self.cy = clamp(l[0], 1, self.height) - 1
        self.cl = 0

    def csi_J(self, l):
        if l[0] == 0:
            self.zero(self.cy, self.cx, self.height - 1, self.width - 1)
        elif l[0] == 1:
            self.zero(0, 0, self.cy, self.cx)
        elif l[0] == 2:
            self.zero(0, 0, self.height - 1, self.width - 1)

    def csi_K(self, l):
        if l[0] == 0:
            self.zero(self.cy, self.cx, self.cy, self.width - 1)
        elif l[0] == 1:
            self.zero(self.cy, 0, self.cy, self.cx)
        elif l[0] == 2:
            self.zero(self.cy, 0, self.cy, self.width - 1)

    def csi_L(self, l):
        if self.cy < self.sb:
            self.scroll_down(self.cy, self.sb, min(l[0], self.sb - self.cy + 1))

    def csi_M(self, l):
        if self.cy >= self.st and self.cy <= self.sb:
            self.scroll_up(self.cy, self.sb, min(l[0], self.sb - self.cy + 1))

    def csi_P(self, l):
        w, cx, cy = self.width, self.cx, self.cy
        end = self.peek(cy, cx, cy, w)
        self.csi_K([0])
        self.poke(cy, cx, end[l[0] :])

    def csi_X(self, l):
        # erase this many characters, at least one, without leaving the row
        last = clamp(self.cx + l[0], self.cx + 1, self.width) - 1
        self.zero(self.cy, self.cx, self.cy, last)

    def csi_a(self, l):
        self.csi_C(l)

    def csi_c(self, l):
        #'\x1b[?0c' 0-8 cursor size
        pass

    def csi_d(self, l):
        self.cy = clamp(l[0], 1, self.height) - 1

    def csi_e(self, l):
        self.csi_B(l)

    def csi_f(self, l):
        self.csi_H(l)

    def csi_h(self, l):
        if l[0] == 4:
            pass

    # 			print "insert on"
    def csi_l(self, l):
        if l[0] == 4:
            pass

    # 			print "insert off"
    def csi_m(self, l):
        for i in l:
            if i == 0 or i == 39 or i == 49 or i == 27:
                self.sgr = DEFAULT_SGR
            elif i == 1:
                self.sgr = self.sgr | 0x0008
            elif i == 7:
                self.sgr = 0x0700
            elif i >= 30 and i <= 37:
                c = i - 30
                self.sgr = (self.sgr & 0xFF08) | c
            elif i >= 40 and i <= 47:
                c = i - 40
                self.sgr = (self.sgr & 0x00FF) | (c << 8)

    # 			else:
    # 				print "CSI sgr ignore",l,i
    # 		print 'sgr: %r %x'%(l,self.sgr)
    def csi_r(self, l):
        # A missing or zero number means the first or last row.
        top, bottom = (l + [0])[:2]
        top = clamp(top, 1, self.height)
        bottom = min(bottom or self.height, self.height)
        if top < bottom:  # otherwise the sequence is invalid, and ignored
            self.st, self.sb = top - 1, bottom - 1

    def csi_s(self, l):
        self.esc_save(0)

    def csi_u(self, l):
        self.esc_restore(0)

    def escape(self):
        e = self.buf
        if len(e) > 32:
            # 			print "error %r"%e
            self.buf = ""
        elif e in self.esc_seq:
            self.esc_seq[e](e)
            self.buf = ""
        else:
            for r, f in self.esc_re:
                mo = r.match(e)
                if mo:
                    f(e, mo)
                    self.buf = ""
                    break

    # 		if self.buf=='': print "ESC %r\n"%e
    def write(self, s):
        s = self.decoder.decode(s)
        for i in s:
            if len(self.buf) or (i in self.esc_seq):
                self.buf += i
                self.escape()
            elif i == "\x1b":
                self.buf += i
            elif i not in UNPRINTABLE:
                self.echo(i)

    def read(self):
        b = self.outbuf
        self.outbuf = ""
        return b

    def dumphtml(self, color=1):
        h = self.height
        w = self.width
        r = ""
        span = ""
        span_bg, span_fg = -1, -1
        for i in range(h * w):
            colors, c = divmod(self.scr[i], 1 << CHAR_BITS)
            if color:
                bg, fg = divmod(colors, 256)
            else:
                bg, fg = 0, 7
            if i == self.cy * w + self.cx:
                bg, fg = 1, 7
            if bg != span_bg or fg != span_fg or i == h * w - 1:
                if len(span):
                    r += f'<span class="f{span_fg:d} b{span_bg:d}">{html.escape(span)}</span>'
                span = ""
                span_bg, span_fg = bg, fg
            span += chr(c)
            if i % w == w - 1:
                span += "\n"
        r = f'<?xml version="1.0" encoding="UTF-8"?><pre class="term">{r}</pre>'
        if self.last_html == r:
            return '<?xml version="1.0"?><idem></idem>'
        else:
            self.last_html = r
            # 			print self
            return r

    def __repr__(self):
        d = self.scr
        r = ""
        for i in range(self.height):
            r += f"|{d[self.width * i : self.width * (i + 1)]}|\n"
        return r


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class SynchronizedMethod:
    def __init__(self, lock, orig):
        self.lock = lock
        self.orig = orig

    def __call__(self, *l):
        with self.lock:
            return self.orig(*l)


class Multiplex:
    def __init__(self, cmd=None, timeout=DEFAULT_TIMEOUT):
        self.cmd = cmd
        self.timeout = timeout
        # Sessions are keyed by an ID that the server chooses, too random to be
        # guessed or, unlike the kernel's file descriptors, to be used again.
        self.proc = {}
        # why a session was closed, by key, when there is a reason to give
        self.close_reasons = {}
        # the sessions whose commands have ended, by key, kept until their pages
        # have received the last screen
        self.last_screens = {}
        # when to kill each closed session's process group, until nothing is left
        self.ending = {}
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=self.loop)
        self.alive = 1
        # synchronize methods
        for name in [
            "create",
            "watch",
            "proc_read",
            "proc_write",
            "proc_flush",
            "expire",
            "reap",
            "dump",
            "die",
            "run",
        ]:
            orig = getattr(self, name)
            setattr(self, name, SynchronizedMethod(self.lock, orig))
        self.thread.start()

    def create(self, w=DEFAULT_WIDTH, h=DEFAULT_HEIGHT):
        """Start a terminal and return its session ID, or None while stopping."""
        if not self.alive:
            return None
        key = secrets.token_hex(16)
        term = Terminal(w, h)
        # Hold signals back while forking, so that the child handles none as this
        # process, or one that includes this application, would.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])  # what's blocked now
        try:
            try:
                # Blocking can run a waiting handler, which may raise.
                signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
                pid, fd = pty.fork()
                if pid == 0:
                    self.exec_command(w, h)  # never returns
                # Record the session first, so that its process gets cleaned up.
                self.proc[key] = {
                    "fd": fd,
                    "pid": pid,
                    "term": term,
                    "input": bytearray(),
                    # when the browser last asked for the screen, and how often
                    "seen": time.monotonic(),
                    "shown": 0,
                }
                fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", h, w, 0, 0))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        except BaseException:
            # Whether setting up failed, or a handler raised when signals were let
            # through again, don't keep a terminal that nobody was given.
            self.proc_close(key)
            raise
        return key

    def exec_command(self, w, h):
        """In a new terminal's process, run the command in its place."""
        name = "the command"
        try:
            # The command starts with the usual handling of every signal, and none
            # blocked, whatever the server inherited or set, or Python chose, such
            # as ignoring broken pipes.
            for signum in signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}:
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(signum, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_SETMASK, [])
            # The command mustn't inherit other terminals or connections.
            try:
                end = max(map(int, os.listdir("/proc/self/fd"))) + 1
            except OSError:
                end = os.sysconf("SC_OPEN_MAX")
            os.closerange(3, end)
            if self.cmd:
                cmd = ["/bin/sh", "-c", self.cmd]
            elif os.getuid() == 0:
                cmd = ["/bin/login"]
            else:
                sys.stdout.write(gethostname() + " login: ")
                sys.stdout.flush()
                login = sys.stdin.readline().strip()
                if re.match("^[0-9A-Za-z-_. ]+$", login):
                    cmd = ["ssh"]
                    cmd += ["-oPreferredAuthentications=keyboard-interactive,password"]
                    cmd += ["-oNoHostAuthenticationForLocalhost=yes"]
                    cmd += ["-oLogLevel=FATAL"]
                    cmd += ["-F/dev/null", "-l", login, "localhost"]
                else:
                    os._exit(0)
            env = {}
            env["COLUMNS"] = str(w)
            env["LINES"] = str(h)
            env["TERM"] = "linux"
            env["PATH"] = os.environ.get("PATH", os.defpath)
            name = cmd[0]
            os.execvpe(cmd[0], cmd, env)
        except BaseException as error:  # noqa: BLE001 (say why, whatever it is)
            reason = f"{type(error).__name__}: {error}"
            message = f"achilterm: couldn't start {name}: {reason}\r\n"
            os.write(2, message.encode(errors="replace"))
        finally:
            # This process is a copy of the server, and mustn't carry on as one.
            os._exit(127)

    def die(self):
        self.alive = 0

    def run(self):
        return self.alive

    def watch(self, selector):
        """Wait for each session's output, and for room for any pending input."""
        for key, proc in self.proc.items():
            events = selectors.EVENT_READ
            if proc["input"]:
                events |= selectors.EVENT_WRITE
            selector.register(proc["fd"], events, key)

    def proc_close(self, key, reason=None, keep_screen=False):
        """End a session. Its page is told the reason, if one is given, after
        receiving the last screen, if that's to be kept."""
        proc = self.proc.pop(key, None)
        if proc:
            if reason:
                self.close_reasons[key] = reason
            if keep_screen:
                # Wait for its page as long as before, counting from now.
                proc.update(seen=time.monotonic(), sent=False)
                proc["input"].clear()  # what the command never read
                self.last_screens[key] = proc
            with contextlib.suppress(OSError):
                os.close(proc["fd"])
            with contextlib.suppress(OSError):
                try:
                    os.killpg(proc["pid"], signal.SIGTERM)
                except ProcessLookupError:  # it doesn't lead its group yet
                    os.kill(proc["pid"], signal.SIGTERM)
            self.ending[proc["pid"]] = time.monotonic() + KILL_DELAY

    def reap(self):
        """Collect the processes of closed sessions, killing any that linger."""
        now = time.monotonic()
        for pid, deadline in list(self.ending.items()):
            try:
                collected = os.waitpid(pid, os.WNOHANG)[0] != 0
            except ChildProcessError:
                collected = True  # earlier, or by something else
            # Also collect the rest of the group, if orphans come to this process,
            # as when it's the first in a container.
            with contextlib.suppress(ChildProcessError):
                while ended := os.waitpid(-pid, os.WNOHANG)[0]:
                    collected = collected or ended == pid
            # The command leads a process group, holding what it started. The
            # group's number isn't used again while anything is left in it.
            try:
                os.killpg(pid, signal.SIGKILL if now > deadline else 0)
            except ProcessLookupError:
                if collected:
                    del self.ending[pid]
                    continue
            except PermissionError:
                pass  # what's left runs as another user
            if now > deadline:
                self.ending[pid] = now + KILL_DELAY

    def idle_limit(self, proc):
        """How long to wait for a session's page to ask for its screen again."""
        if proc["shown"] < 2:
            # Only the request that started it has asked: the reply with its ID was
            # probably lost, so don't wait as long.
            return min(self.timeout, MINIMUM_TIMEOUT)
        return self.timeout

    def expire(self):
        """Close the sessions that no browser is showing any more."""
        now = time.monotonic()
        for key, proc in list(self.proc.items()):
            if now - proc["seen"] > self.idle_limit(proc):
                self.proc_close(key, "because the page was disconnected")
        for key, proc in list(self.last_screens.items()):
            if now - proc["seen"] > self.idle_limit(proc):
                del self.last_screens[key]

    def proc_read(self, key):
        proc = self.proc.get(key)
        if proc is None:
            return
        try:
            data = os.read(proc["fd"], 65536)
        except BlockingIOError:
            return  # there's nothing to read after all
        except OSError:
            data = b""
        if not data:
            # The command has ended. Reading then fails on Linux, but on macOS and
            # BSD every read returns nothing. Its page may not have seen the last
            # of its output yet, so keep the screen.
            self.proc_close(key, keep_screen=True)
            return
        try:
            term = proc["term"]
            term.write(data)
            reply = term.read()
            if reply:
                self.proc_write(key, reply)
        except Exception:
            # A fault in one terminal mustn't stop the others.
            logger.exception("Closing a terminal whose output couldn't be handled")
            self.proc_close(key, "because of a server error")

    def proc_write(self, key, s):
        if key in self.proc:
            self.proc[key]["input"] += s.encode("utf-8")
            self.proc_flush(key)

    def proc_flush(self, key):
        # The terminal may accept only part of a large input, such as a paste,
        # so keep the rest until it can take more.
        proc = self.proc.get(key)
        if proc and proc["input"]:
            try:
                del proc["input"][: os.write(proc["fd"], proc["input"])]
            except BlockingIOError:
                pass
            except OSError:
                # The command has probably ended. Leave closing to proc_read,
                # which first reads the rest of its output.
                proc["input"].clear()

    def dump(self, key, color=1, full=False):
        """The screen to send a session's page, or False if it has ended."""
        proc = self.proc.get(key)
        if proc is None:
            # Send a command's last screen until the page shows that it arrived,
            # by asking again without wanting the whole screen once more.
            proc = self.last_screens.get(key)
            if proc is None or (proc["sent"] and not full):
                self.last_screens.pop(key, None)
                return False
            proc["sent"] = True
        proc["seen"] = time.monotonic()
        proc["shown"] += 1
        try:
            term = proc["term"]
            if full:
                term.last_html = ""  # send the screen even if it hasn't changed
            return term.dumphtml(color)
        except Exception:
            logger.exception("Closing a terminal whose screen couldn't be drawn")
            self.proc_close(key)
            self.last_screens.pop(key, None)
            self.close_reasons[key] = "because of a server error"
            return False

    def loop(self):
        while self.run():
            self.expire()
            self.reap()
            # Unlike select(), poll() works with any number of descriptors.
            try:
                with selectors.PollSelector() as selector:
                    self.watch(selector)
                    ready = selector.select(1.0)
            except OSError:
                # Keep trying: if this thread ended, every terminal would freeze.
                logger.exception("Couldn't wait for terminal output")
                time.sleep(1)
                continue
            # A session may have ended while waiting. Its key no longer matches
            # anything, even if a new session has its file descriptor.
            for item, events in ready:
                if events & selectors.EVENT_WRITE:
                    self.proc_flush(item.data)
                if events & selectors.EVENT_READ:
                    self.proc_read(item.data)
            if any(events & selectors.EVENT_READ for item, events in ready):
                time.sleep(0.002)
        # Requests may still be ending sessions while the server stops.
        with self.lock:
            for key in list(self.proc):
                self.proc_close(key)
        # Wait for the processes to end, but not forever for one that can't be
        # killed, such as a command running as another user.
        give_up = time.monotonic() + KILL_DELAY + 1
        while self.ending and time.monotonic() < give_up:
            time.sleep(0.05)
            self.reap()


class AchilTerm:
    def __init__(
        self,
        cmd=None,
        index_file=None,
        *,
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        timeout=DEFAULT_TIMEOUT,
        allow_hosts=(),
    ):
        # These go into the page's script, so must be sizes it can be given.
        width = whole_number(width, "width", WIDTHS)
        height = whole_number(height, "height", HEIGHTS)
        os.chdir(os.path.normpath(os.path.dirname(__file__)))
        self.files = {}
        for i in ["css", "html", "js"]:
            for j in glob.glob(f"*.{i}"):
                with open(j) as f:
                    self.files[j] = f.read()
        with open(index_file or "achilterm.html") as f:
            self.files["index"] = f.read() % {"width": width, "height": height}
        self.mime = mimetypes.types_map.copy()
        self.mime[".html"] = "text/html; charset=UTF-8"
        # Older Pythons map .js to a type that WebOb won't write text to.
        self.mime[".js"] = "text/javascript; charset=UTF-8"
        self.hosts = LOCAL_HOSTS | {allowed_host(host) for host in allow_hosts}
        self.multi = Multiplex(cmd, timeout)

    def refusal(self, req, to_terminal):
        """The response to a request that mustn't be served, or None."""
        if host_name(req.environ.get("HTTP_HOST", "")) not in self.hosts:
            return refuse(403, "Unknown host name. Start achilterm with --allow-host.")
        if not to_terminal:
            return None
        # Keystrokes are only accepted in a request body, because servers and
        # proxies commonly log addresses.
        if req.method != "POST":
            return refuse(405, "Use POST.", Allow="POST")
        # Another website's page can't send this header without permission, which
        # is never given.
        if "X-Achilterm" not in req.headers:
            return refuse(403, "Missing X-Achilterm header. Reload the page.")
        # The page sends a URL-encoded form in UTF-8. Reading other kinds can fail,
        # or give uploaded files where text is expected.
        form = "application/x-www-form-urlencoded"
        if (req.content_type, req.charset) != (form, "UTF-8"):
            return refuse(415, "Send a URL-encoded UTF-8 form. Reload the page.")
        try:
            fields = req.POST
        except DisconnectionError:
            # The page tries again after this status.
            return refuse(408, "The request was cut off.")
        # Only an empty session ID asks for a new terminal, not a missing one.
        if "s" not in fields:
            return refuse(400, "Missing session ID. Reload the page.")
        return None

    def __call__(self, environ, start_response):
        req = webob.Request(environ)
        to_terminal = req.environ["PATH_INFO"].endswith("/u")
        refused = self.refusal(req, to_terminal)
        if refused:
            return refused(environ, start_response)
        res = webob.Response()
        if to_terminal:
            session = req.POST["s"]
            k = req.POST.get("k", "")
            c = req.POST.get("c", "")
            if not session:
                # A page that names no session is new, and gets a terminal.
                session = self.multi.create(*requested_size(req.POST))
                if session is None:
                    refused = refuse(503, "The server is stopping.")
                    return refused(environ, start_response)
            if k:
                self.multi.proc_write(session, k)
            time.sleep(0.002)
            # A page that lost a response asks for the whole screen again.
            dump = self.multi.dump(session, c, bool(req.POST.get("f")))
            if dump is False:
                # Don't start another terminal in its place: the person may go on
                # typing as if it were the one that ended.
                reason = self.multi.close_reasons.get(session)
                ended = " ".join(
                    filter(None, ["This terminal session has ended", reason])
                )
                refused = refuse(410, f"{ended}. Reload the page to start a new one.")
                return refused(environ, start_response)
            res.content_type = "text/xml"
            res.headers["X-Achilterm-Session"] = session
            res.content_encoding = "gzip"
            res.write(gzip.compress(dump.encode("utf-8")))
        else:
            # Another website mustn't show the terminal inside its own page, where
            # it could trick someone into typing into it.
            res.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
            res.headers["X-Frame-Options"] = "SAMEORIGIN"
            n = os.path.basename(req.environ["PATH_INFO"])
            if n in self.files:
                res.content_type = self.mime.get(
                    os.path.splitext(n)[1].lower(), "application/octet-stream"
                )
                res.write(self.files[n])
            else:
                res.content_type = "text/html; charset=UTF-8"
                res.write(self.files["index"])
        return res(environ, start_response)


class NoLogWSGIRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(usage="%(prog)s [options]")
    parser.add_argument(
        "--version", action="version", version="Achilterm version " + __version__
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8022,
        help="set the TCP port (default: %(default)s)",
    )
    parser.add_argument(
        "-c",
        "--command",
        dest="cmd",
        help="set the command (default: /bin/login or ssh localhost)",
    )
    parser.add_argument(
        "-l",
        "--log",
        action="store_true",
        help="log requests to stderr (default: quiet mode)",
    )
    parser.add_argument(
        "-i",
        "--index",
        dest="index_file",
        help="default index file (default: achilterm.html)",
    )
    parser.add_argument(
        "-w",
        "--width",
        default=DEFAULT_WIDTH,
        help=f"set the width, from {WIDTHS[0]} to {WIDTHS[-1]} (default: %(default)s)",
    )
    parser.add_argument(
        "-H",
        "--height",
        default=DEFAULT_HEIGHT,
        help=f"set the height, from {HEIGHTS[0]} to {HEIGHTS[-1]} "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="close a terminal not shown by a browser for this many seconds "
        f"(default: %(default)s, at least {MINIMUM_TIMEOUT})",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        dest="allow_hosts",
        default=[],
        metavar="NAME",
        help="also accept requests for this host name; repeat for more "
        "(default: only localhost)",
    )
    args = parser.parse_args()
    if args.port not in PORTS:
        parser.error(f"--port must be from {PORTS[0]} to {PORTS[-1]}")
    if args.timeout < MINIMUM_TIMEOUT:
        parser.error(f"--timeout must be at least {MINIMUM_TIMEOUT} seconds")
    try:
        width = whole_number(args.width, "--width", WIDTHS)
        height = whole_number(args.height, "--height", HEIGHTS)
    except ValueError as error:
        parser.error(str(error))
    for host in args.allow_hosts:
        try:
            allowed_host(host)
        except ValueError as error:
            parser.error(f"--allow-host: {error}")
    if args.log:
        handler_class = WSGIRequestHandler
    else:
        handler_class = NoLogWSGIRequestHandler
    # Close the terminals when stopped, as by Ctrl-C, kill or closing the terminal
    # running the server, unless a signal is meant to be ignored, as hangups are
    # under nohup. Block these signals before starting any threads, which inherit
    # that, so that they interrupt nothing and one thread can wait for them.
    stop_signals = {
        signum for signum in STOP_SIGNALS if signal.getsignal(signum) != signal.SIG_IGN
    }
    signal.pthread_sigmask(signal.SIG_BLOCK, stop_signals)
    index_file = os.path.abspath(args.index_file) if args.index_file else None
    at = AchilTerm(
        args.cmd,
        index_file=index_file,
        width=width,
        height=height,
        timeout=args.timeout,
        allow_hosts=args.allow_hosts,
    )

    try:
        # Closing the server before the terminals refuses requests made meanwhile.
        with make_server(
            "localhost",
            args.port,
            at,
            handler_class=handler_class,
            server_class=ThreadingWSGIServer,
        ) as server:

            def announce():
                # Written straight to standard output, so that it appears at once,
                # even in a file. Being unable to write it mustn't stop the server.
                address = f"Achilterm at http://localhost:{server.server_port}/\n"
                with contextlib.suppress(OSError):
                    os.write(1, address.encode())

            if sys.stdout:  # otherwise descriptor 1 may be something else by now
                # in its own thread, so that output nobody is reading delays nothing
                threading.Thread(target=announce, daemon=True).start()

            def stop():
                signal.sigwait(stop_signals)
                server.shutdown()

            threading.Thread(target=stop, daemon=True).start()
            server.serve_forever()
    finally:
        at.multi.die()
        at.multi.thread.join()


if __name__ == "__main__":
    main()
