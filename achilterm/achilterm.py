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

import array
import codecs
import contextlib
import fcntl
import glob
import gzip
import html
import logging
import mimetypes
import optparse
import os
import pty
import pwd
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
        # signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        self.cmd = cmd
        self.timeout = timeout
        # Sessions are keyed by an ID that the server chooses, too random to be
        # guessed or, unlike the kernel's file descriptors, to be used again.
        self.proc = {}
        # why a session was closed, by key, when there is a reason to give
        self.close_reasons = {}
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
            "dump",
            "die",
            "run",
        ]:
            orig = getattr(self, name)
            setattr(self, name, SynchronizedMethod(self.lock, orig))
        self.thread.start()

    def create(self, w=80, h=25):
        pid, fd = pty.fork()
        if pid == 0:
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
            env["PATH"] = os.environ["PATH"]
            os.execvpe(cmd[0], cmd, env)
        else:
            fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", h, w, 0, 0))
            key = secrets.token_hex(16)
            self.proc[key] = {
                "fd": fd,
                "pid": pid,
                "term": Terminal(w, h),
                "input": bytearray(),
                # when the browser last asked for the screen, and how often
                "seen": time.monotonic(),
                "shown": 0,
            }
            return key

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

    def proc_close(self, key, reason=None):
        """End a session. Its page is told the reason, if one is given."""
        proc = self.proc.pop(key, None)
        if proc:
            if reason:
                self.close_reasons[key] = reason
            try:
                os.close(proc["fd"])
                os.kill(proc["pid"], signal.SIGTERM)
            except OSError:
                pass

    def expire(self):
        """Close the sessions that no browser is showing any more."""
        now = time.monotonic()
        for key, proc in list(self.proc.items()):
            timeout = self.timeout
            if proc["shown"] < 2:
                # Only the request that started it has asked: the reply with its ID
                # was probably lost, so don't keep it as long.
                timeout = min(timeout, MINIMUM_TIMEOUT)
            if proc["seen"] < now - timeout:
                self.proc_close(key, "because the page was disconnected")

    def proc_read(self, key):
        try:
            proc = self.proc[key]
            data = os.read(proc["fd"], 65536)
        except (KeyError, OSError):
            self.proc_close(key)
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
        try:
            proc = self.proc[key]
            if proc["input"]:
                del proc["input"][: os.write(proc["fd"], proc["input"])]
        except BlockingIOError:
            pass
        except (KeyError, OSError):
            self.proc_close(key)

    def dump(self, key, color=1, full=False):
        proc = self.proc.get(key)
        if proc is None:
            return False
        proc["seen"] = time.monotonic()
        proc["shown"] += 1
        try:
            term = proc["term"]
            if full:
                term.last_html = ""  # send the screen even if it hasn't changed
            return term.dumphtml(color)
        except Exception:
            logger.exception("Closing a terminal whose screen couldn't be drawn")
            self.proc_close(key, "because of a server error")
            return False

    def loop(self):
        while self.run():
            self.expire()
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


class AchilTerm:
    def __init__(
        self,
        cmd=None,
        index_file=None,
        width=80,
        height=25,
        timeout=DEFAULT_TIMEOUT,
        allow_hosts=(),
    ):
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
        # Only an empty session ID asks for a new terminal, not a missing one.
        if "s" not in req.POST:
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
                w = int(req.POST.get("w", 0))
                h = int(req.POST.get("h", 0))
                if not (w > 2 and w < 256 and h > 2 and h < 100):
                    w, h = 80, 25
                session = self.multi.create(w, h)
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
    parser = optparse.OptionParser(version="Achilterm version " + __version__)
    parser.add_option(
        "-p",
        "--port",
        dest="port",
        default="8022",
        help="set the TCP port (default: 8022)",
    )
    parser.add_option(
        "-c",
        "--command",
        dest="cmd",
        default=None,
        help="set the command (default: /bin/login or ssh localhost)",
    )
    parser.add_option(
        "-l",
        "--log",
        action="store_true",
        dest="log",
        default=0,
        help="log requests to stderr (default: quiet mode)",
    )
    parser.add_option(
        "-d",
        "--daemon",
        action="store_true",
        dest="daemon",
        default=0,
        help="run as daemon in the background",
    )
    parser.add_option(
        "-P",
        "--pidfile",
        dest="pidfile",
        default="/var/run/achilterm.pid",
        help="set the pidfile (default: /var/run/achilterm.pid)",
    )
    parser.add_option(
        "-i",
        "--index",
        dest="index_file",
        default=0,
        help="default index file (default: achilterm.html)",
    )
    parser.add_option("-u", "--uid", dest="uid", help="set the daemon's user id")
    parser.add_option(
        "-w", "--width", dest="width", default="80", help="set the width (default: 80)"
    )
    parser.add_option(
        "-H",
        "--height",
        dest="height",
        default="25",
        help="set the height (default: 25)",
    )
    parser.add_option(
        "--timeout",
        type="int",
        dest="timeout",
        default=DEFAULT_TIMEOUT,
        help="close a terminal not shown by a browser for this many seconds "
        f"(default: %default, at least {MINIMUM_TIMEOUT})",
    )
    parser.add_option(
        "--allow-host",
        action="append",
        dest="allow_hosts",
        default=[],
        metavar="NAME",
        help="also accept requests for this host name; repeat for more "
        "(default: only localhost)",
    )
    (o, _a) = parser.parse_args()
    if o.timeout < MINIMUM_TIMEOUT:
        parser.error(f"--timeout must be at least {MINIMUM_TIMEOUT} seconds")
    for host in o.allow_hosts:
        try:
            allowed_host(host)
        except ValueError as error:
            parser.error(f"--allow-host: {error}")
    if o.daemon:
        pid = os.fork()
        if pid == 0:
            # os.setsid() ?
            os.setpgrp()
            with open(os.devnull) as nullin, open(os.devnull, "w") as nullout:
                os.dup2(nullin.fileno(), sys.stdin.fileno())
                os.dup2(nullout.fileno(), sys.stdout.fileno())
                os.dup2(nullout.fileno(), sys.stderr.fileno())
            if os.getuid() == 0 and o.uid:
                try:
                    os.setuid(int(o.uid))
                except (ValueError, OSError):
                    os.setuid(pwd.getpwnam(o.uid).pw_uid)
        else:
            with contextlib.suppress(OSError), open(o.pidfile, "w+") as pidfile:
                pidfile.write(str(pid) + "\n")
            print(f"Achilterm at http://localhost:{o.port}/ pid: {pid:d}")
            sys.exit(0)
    else:
        print(f"Achilterm at http://localhost:{o.port}/")
    if o.log:
        handler_class = WSGIRequestHandler
    else:
        handler_class = NoLogWSGIRequestHandler
    index_file = os.path.abspath(o.index_file) if o.index_file else None
    at = AchilTerm(
        o.cmd,
        index_file=index_file,
        width=o.width,
        height=o.height,
        timeout=o.timeout,
        allow_hosts=o.allow_hosts,
    )
    try:
        make_server(
            "localhost",
            int(o.port),
            at,
            handler_class=handler_class,
            server_class=ThreadingWSGIServer,
        ).serve_forever()
    except KeyboardInterrupt:
        sys.excepthook(*sys.exc_info())
    at.multi.die()


if __name__ == "__main__":
    main()
