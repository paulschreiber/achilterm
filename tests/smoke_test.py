"""Start the installed achilterm command and exercise it over HTTP."""

import contextlib
import ctypes
import functools
import gzip
import io
import os
import random
import re
import resource
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from importlib.metadata import version
from unittest import mock
from wsgiref.simple_server import make_server
from xml.etree import ElementTree

import webob

from achilterm.achilterm import (
    DEFAULT_TIMEOUT,
    MINIMUM_TIMEOUT,
    STOP_SIGNALS,
    AchilTerm,
    NoLogWSGIRequestHandler,
    RequestHandler,
    Terminal,
    ThreadingWSGIServer,
    requested_size,
)

COMMAND = os.path.join(os.path.dirname(sys.executable), "achilterm")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# The header the terminal page sends with its requests.
PAGE_HEADERS = {"X-Achilterm": "1"}


def exchange(url, data=None, headers=PAGE_HEADERS):
    """The headers and text of a response."""
    request = urllib.request.Request(url, data, headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return response.headers, body.decode()


def fetch(url, data=None, headers=PAGE_HEADERS):
    return exchange(url, data, headers)[1]


def status(url, data=None, headers=PAGE_HEADERS):
    try:
        fetch(url, data, headers)
    except urllib.error.HTTPError as error:
        return error.code
    return 200


def terminal_query(session="", keys="", **extra):
    """A page's request. An empty session asks for a new terminal."""
    fields = {"s": session, "w": 80, "h": 25, "c": 1, "k": keys, **extra}
    return urllib.parse.urlencode(fields).encode()


def new_session(url):
    return exchange(url, terminal_query())[0]["X-Achilterm-Session"]


def refusal(url, data):
    """The status and text of a response refusing a request."""
    try:
        fetch(url, data)
    except urllib.error.HTTPError as error:
        assert error.headers.get_content_type() == "text/plain", error.headers
        return error.code, error.read().decode()
    raise AssertionError(f"{data!r} was accepted")


def is_running(pid):
    try:
        with open(f"/proc/{pid}/stat") as stat:
            # An ended process stays as a zombie until it is collected.
            return stat.read().rpartition(")")[2].split()[0] != "Z"
    except FileNotFoundError:
        return False


def buffered_environment():
    """This process's environment, leaving Python's output buffered as usual."""
    return {
        name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"
    }


def announced_address(server):
    """The line a server writes as soon as it's listening."""
    assert select.select([server.stdout], [], [], 5)[0], "no address printed"
    line = server.stdout.readline().decode()
    assert line, "the server ended without printing its address"
    return line


def terminate(server):
    """Stop a server and return its output, never leaving it running."""
    server.terminate()
    try:
        return server.communicate(timeout=10)
    finally:
        server.kill()


def start_server(*options):
    port = free_port()
    base = f"http://127.0.0.1:{port}/"
    server = subprocess.Popen(
        [COMMAND, f"--port={port}", "--command=exec /bin/sh", *options],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=buffered_environment(),
    )
    for attempt in range(50):
        try:
            fetch(base)
            return server, port, base
        except OSError:
            time.sleep(0.1)
    server.kill()
    raise AssertionError("server did not start")


def check_access():
    # Leading zeros don't make a number octal, which would make 0100 less than 90.
    server, port, base = start_server(
        "--allow-host=Terminal.Example.com:443", "--timeout=0100"
    )
    url = f"{base}u"
    try:
        # Only requests from the terminal page.
        assert status(url, terminal_query(), headers={}) == 403

        # The server chooses a new terminal's session ID, and only an ID it
        # issued reaches a terminal. One from before a restart, say, is refused.
        session = new_session(url)
        assert re.fullmatch("[0-9a-f]{32}", session), session
        assert new_session(url) != session
        assert status(url, urllib.parse.urlencode({"k": ""}).encode()) == 400
        for unknown in ("1", "a" * 32, session[:-1], session + "a"):
            code, text = refusal(url, terminal_query(unknown))
            assert code == 410, (unknown, code)
            assert "session has ended. Reload" in text, text

        # Only for localhost and the names that were allowed.
        for host, expected in (
            (f"localhost:{port}", 200),
            ("[::1]", 200),
            ("terminal.example.com", 200),
            ("attacker.example", 403),
            ("localhost.attacker.example", 403),
            ("", 403),
        ):
            headers = {**PAGE_HEADERS, "Host": host}
            assert status(base, headers=headers) == expected, host
            assert status(url, terminal_query(session), headers) == expected, host

        # The whole screen is sent again when asked for, even if unchanged.
        time.sleep(1)
        fetch(url, terminal_query(session))
        assert "<idem>" in fetch(url, terminal_query(session))
        assert "<pre" in fetch(url, terminal_query(session, f=1))

        # Other websites may not frame the page.
        with urllib.request.urlopen(base, timeout=10) as response:
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    finally:
        errors = terminate(server)[1].decode()
    assert "Traceback" not in errors, errors

    for option in (
        "unexpected",
        "--timeout=89",
        "--timeout=0x100",
        "--timeout=300.5",
        "--port=abc",
        "--port=",
        "--port=-1",
        "--port=65536",
        "--width=abc",
        "--width=2",
        "--height=100",
        "--allow-host=[nonsense",
        "--allow-host=https://terminal.example.com",
        "--allow-host=*.example.com",
        "--allow-host=a.example,b.example",
    ):
        result = subprocess.run(
            [COMMAND, option], capture_output=True, timeout=10, check=False
        )
        assert result.returncode == 2, (option, result)
        assert b"Traceback" not in result.stderr, (option, result)


def check_escape_sequences():
    """No sequence may move the cursor or scroll region off the screen."""
    # A zero or missing row means the first or last, and a region whose top
    # isn't above its bottom is ignored.
    for sequence, region in (
        ("[r", (0, 24)),
        ("[0;0r", (0, 24)),
        ("[1;0r", (0, 24)),
        ("[3r", (2, 24)),
        ("[5;20r", (4, 19)),
        ("[20;5r", (1, 9)),
        ("[99;99r", (1, 9)),
    ):
        term = Terminal(80, 25)
        term.write(f"\x1b[2;10r\x1b{sequence}".encode())
        assert (term.st, term.sb) == region, (sequence, term.st, term.sb)

    # Lines and characters are inserted and deleted within the region and row.
    for sequence, rows in (
        ("[2;1H\x1b[L", "abcd ____ efgh ijkl"),
        ("[2;1H\x1b[2L", "abcd ____ ____ efgh"),
        ("[2;1H\x1b[999L", "abcd ____ ____ ____"),
        ("[2;1H\x1b[2M", "abcd mnop ____ ____"),
        ("[2;1H\x1b[999M", "abcd ____ ____ ____"),
        ("[2;3H\x1b[@", "abcd ef_g ijkl mnop"),
        ("[2;3H\x1b[999@", "abcd ef__ ijkl mnop"),
        ("[2;2H\x1b[X", "abcd e_gh ijkl mnop"),
        ("[2;2H\x1b[999X", "abcd e___ ijkl mnop"),
    ):
        term = Terminal(4, 4)
        term.write(f"abcdefghijklmnop\x1b{sequence}".encode())
        text = "".join(chr(cell & 0xFFFFFF) for cell in term.scr).replace(" ", "_")
        shown = " ".join(text[start : start + 4] for start in range(0, 16, 4))
        assert shown == rows, (sequence, shown)

    numbers = ("", "0", "1", "3", "25", "26", "80", "81", "999")
    parts = ["a", "bbbbbbbbbbbb", "\n", "\r", "\t", "\x08", "\u20ac"]
    parts += ["\x1b7", "\x1b8", "\x1bM", "\x1bc"]
    finals = "@ABCDEFGHJKLMPXacdefhlmrsu`"
    rng = random.Random(1)
    # sequences that have caused trouble, then random ones
    cases = [["\x1b[0;0r\x1b[0d\n"], ["\x1b[r"] + ["a\n"] * 40, ["\x1b[0;0H\x1b[K"]]
    cases += [["\x1b[5;80H\x1b[9@"], ["\x1b[25;80H\x1b[999X"]]
    for trial in range(3000):
        case = []
        for step in range(20):
            if rng.choice((True, False)):
                case.append(rng.choice(parts))
            else:
                values = ";".join(rng.choices(numbers, k=rng.randrange(4)))
                case.append(f"\x1b[{values}{rng.choice(finals)}")
        cases.append(case)
    for case in cases:
        for width, height in ((3, 3), (80, 25)):
            term = Terminal(width, height)
            for text in case:
                term.write(text.encode())
                assert len(term.scr) == width * height, case
                assert 0 <= term.cx < width and 0 <= term.cy < height, case
                assert 0 <= term.st <= term.sb < height, case
            ElementTree.fromstring(term.dumphtml().encode())


def post(app, session="", keys="", **extra):
    """Ask an application running here for a terminal's screen."""
    query = terminal_query(session, keys, **extra)
    request = webob.Request.blank("/u", headers=PAGE_HEADERS, POST=query)
    return request.get_response(app)


def new_app_session(app):
    response = post(app)
    assert response.status_code == 200, response.status
    return response.headers["X-Achilterm-Session"]


@contextlib.contextmanager
def running_app(*args, **options):
    app = AchilTerm(*args, **options)
    try:
        yield app
    finally:
        app.multi.die()
        app.multi.thread.join(10)


def wait_for_screen(send, keys, pattern):
    """Send keys, then wait until the screen matches the pattern."""
    for attempt in range(50):
        # A response only holds the screen when it has changed, so check every
        # one, including the response to the keys.
        screen = send(keys if attempt == 0 else "")
        if "<pre" in screen and re.search(pattern, screen_text(screen)):
            return screen
        time.sleep(0.1)
    raise AssertionError(f"{pattern!r} never appeared: {screen}")


def check_expiry():
    # Runs the application here, to use a timeout shorter than the command allows.
    with running_app("while :; do echo busy; sleep 1; done", timeout=2) as app:
        sessions, pids = {}, {}
        for name in ("shown", "left"):
            sessions[name] = new_app_session(app)
            pids[name] = app.multi.proc[sessions[name]]["pid"]
            assert post(app, sessions[name]).status_code == 200
        # A terminal closes once no page asks for it, even while it has output,
        # and stays open while one does.
        for attempt in range(50):
            time.sleep(0.2)
            assert post(app, sessions["shown"]).status_code == 200
            # Go on past the timeout, so that "shown" has to outlast it.
            if attempt > 12 and not is_running(pids["left"]):
                break
        assert is_running(pids["shown"]), "a shown terminal was closed"
        assert not is_running(pids["left"]), "an abandoned terminal stayed open"
        # Its page is told, every time, and gets no new terminal.
        for attempt in range(2):
            response = post(app, sessions["left"])
            assert response.status_code == 410, response.status
            assert "disconnected. Reload" in response.text, response.text
        assert len(app.multi.proc) == 1, app.multi.proc


def check_unused():
    """A new terminal closes soon if its page never asks for it again."""
    with running_app("sleep 60") as app:
        unused, used = new_app_session(app), new_app_session(app)
        assert post(app, used).status_code == 200

        def age(seconds):
            for proc in app.multi.proc.values():
                proc["seen"] -= seconds
            app.multi.expire()

        with app.multi.lock:
            # A slowed page may take this long to ask again.
            age(MINIMUM_TIMEOUT - 10)
            assert list(app.multi.proc) == [unused, used]
            age(11)
            assert list(app.multi.proc) == [used]

    # However long the timeout, the clock arithmetic copes, both for open
    # terminals and for the last screens of ended ones.
    with running_app("read line", timeout=10**400) as app:
        ended, running = new_app_session(app), new_app_session(app)
        for session in (ended, running):
            assert post(app, session).status_code == 200
        post(app, ended, "\r")
        assert wait_until(lambda: ended in app.multi.last_screens)
        app.multi.expire()
        assert list(app.multi.proc) == [running], app.multi.proc


def check_output_fault():
    """If one terminal's output can't be handled, only that one is closed."""

    def fail(*args):
        raise RuntimeError("deliberate failure for this test")

    with running_app("while :; do echo busy; sleep 1; done") as app:
        other = new_app_session(app)
        # reading the output, then drawing the screen
        for method in ("write", "dumphtml"):
            session = new_app_session(app)
            # The error is expected, so collect its log entry instead of showing it.
            with unittest.TestCase().assertLogs("achilterm.achilterm") as logs:
                setattr(app.multi.proc[session]["term"], method, fail)
                for attempt in range(50):
                    time.sleep(0.2)
                    response = post(app, session)
                    if response.status_code != 200:
                        break
            assert response.status_code == 410, response.status
            assert "server error" in response.text, response.text
            errors = [record.exc_info[0] for record in logs.records]
            assert errors == [RuntimeError], errors
        assert post(app, other).status_code == 200
        assert app.multi.thread.is_alive()


def app_screen(app, session, keys=""):
    """Send keys to an application running here, and return its response."""
    response = post(app, session, keys)
    response.decode_content()
    return response.text


def is_collected(pid):
    """Whether a process is gone altogether, not left as a zombie."""
    return not os.path.exists(f"/proc/{pid}")


def wait_until(condition, seconds=5):
    end = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > end:
            return False
        time.sleep(0.05)
    return True


def check_processes():
    """Closed terminals leave no processes behind, even ones that won't end."""
    # Have orphans come to this process, as they do to the first in a container,
    # so that they must be collected too.
    prctl = ctypes.CDLL(None).prctl
    set_child_subreaper = 36
    assert prctl(set_child_subreaper, 1, 0, 0, 0) == 0
    try:
        with mock.patch("achilterm.achilterm.KILL_DELAY", 1):
            check_cleanup()
    finally:
        prctl(set_child_subreaper, 0, 0, 0, 0)


def check_cleanup():
    command = """read mode
        [ $mode = exit ] && exit
        [ $mode = stubborn ] && trap '' HUP TERM
        [ $mode = child ] && trap '' HUP TERM
        sleep 60 &
        [ $mode = child ] && trap - HUP TERM
        echo "child $!"; wait"""

    def start(app, mode):
        """A session, its process and that process's own child."""
        session = new_app_session(app)
        pid = app.multi.proc[session]["pid"]
        send = functools.partial(app_screen, app, session)
        screen = wait_for_screen(send, mode + "\r", "child [0-9]+")
        child = int(re.search("child ([0-9]+)", screen_text(screen))[1])
        return session, pid, child

    def close(app, session):
        with app.multi.lock:
            app.multi.proc_close(session)

    def ends(pid, child, seconds=5):
        return wait_until(
            lambda: is_collected(pid) and is_collected(child), seconds=seconds
        )

    with running_app(command) as app:
        session = new_app_session(app)
        pid = app.multi.proc[session]["pid"]
        post(app, session, "exit\r")
        assert wait_until(functools.partial(is_collected, pid)), "exited"

        session, pid, child = start(app, "ordinary")
        close(app, session)
        assert ends(pid, child), "closed"

        # The command, or only something it started, ignores signals.
        for mode in ("stubborn", "child"):
            session, pid, child = start(app, mode)
            close(app, session)
            time.sleep(0.3)
            assert is_running(child), f"{mode}: killed too soon"
            assert ends(pid, child), f"{mode}: survived"
        assert wait_until(lambda: not app.multi.ending), app.multi.ending

        session, pid, child = start(app, "stubborn")
    # Stopping the server ends them too, and no more are started.
    assert ends(pid, child, 1), "outlived the server"
    assert post(app).status_code == 503
    assert not app.multi.proc


def connection_refused(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except ConnectionRefusedError:
        return True
    except (ConnectionResetError, TimeoutError):
        return False  # caught as it closed, so try again


def check_stop(stop, ignored_by_command="HUP"):
    """Stopping the server with a signal closes its terminals."""
    command = (
        f"grep -E '^Sig(Ign|Blk)' /proc/self/status; trap '' {ignored_by_command};"
        ' echo "pid $$"; exec sleep 60'
    )
    # The server leaves a signal alone if it inherits ignoring it, as it does
    # SIGQUIT here, though its commands mustn't.
    inherited = {
        stop: signal.signal(stop, signal.SIG_DFL),
        signal.SIGQUIT: signal.signal(signal.SIGQUIT, signal.SIG_IGN),
    }
    try:
        server, port, base = start_server(f"--command={command}")
    finally:
        for signum, handler in inherited.items():
            signal.signal(signum, handler)

    def send(session, keys):
        return fetch(f"{base}u", terminal_query(session, keys))

    try:
        announced = announced_address(server)
        assert announced == f"Achilterm at http://localhost:{port}/\n", announced
        show = functools.partial(send, new_session(f"{base}u"))
        screen = screen_text(wait_for_screen(show, "", "pid [0-9]+"))
        pid = int(re.search("pid ([0-9]+)", screen)[1])
        # The command ignores and blocks no signals, whatever the server does.
        for name in ("SigIgn", "SigBlk"):
            mask = int(re.search(f"{name}:\\s+([0-9a-f]+)", screen)[1], 16)
            others = (signal.SIGQUIT, signal.SIGPIPE, signal.SIGXFSZ)
            for signum in (*STOP_SIGNALS, *others):
                assert not mask >> (signum - 1) & 1, (name, signum, hex(mask))
        # A second server can't use the same port, and mustn't hang.
        second = subprocess.run(
            [COMMAND, f"--port={port}"], check=False, capture_output=True, timeout=10
        )
        assert second.returncode != 0, second
        assert b"Achilterm at" not in second.stdout, "claimed to be running"
        server.send_signal(stop)
        if "TERM" in ignored_by_command:
            # While it waits to kill the command, the server takes no connections.
            refused = functools.partial(connection_refused, port)
            assert wait_until(refused, 2), "still listening"
            assert server.poll() is None, "didn't wait to kill the command"
        # More signals, including while it waits to kill a command, mustn't cut
        # that short or cause errors.
        for attempt in range(20):
            for signum in STOP_SIGNALS:
                server.send_signal(signum)
            if server.poll() is not None:
                break
            time.sleep(0.05)
        errors = server.communicate(timeout=10)[1].decode()
        assert server.returncode == 0, (stop, server.returncode, errors)
        assert "Traceback" not in errors, errors
        assert wait_until(lambda: not is_running(pid)), f"outlived {stop!r}"
    finally:
        server.kill()
        server.communicate(timeout=10)


def check_end_of_output():
    """A terminal closes when a read returns nothing, as on macOS and BSD, but not
    when there's nothing to read yet."""
    with running_app("sleep 30") as app:
        session = new_app_session(app)
        # Hold the lock so that only these reads happen.
        with app.multi.lock:
            # Failing to send input leaves the output still to be read.
            with mock.patch("os.write", side_effect=OSError):
                app.multi.proc_write(session, "x")
            assert session in app.multi.proc, "closed before its output was read"
            assert not app.multi.proc[session]["input"]
            # Having nothing to read yet isn't the end.
            with mock.patch("os.read", side_effect=BlockingIOError):
                app.multi.proc_read(session)
            assert session in app.multi.proc, "a live terminal was closed"
            with mock.patch("os.read", return_value=b""):
                app.multi.proc_read(session)
            assert session not in app.multi.proc, "the terminal stayed open"
        screens_until_ended(app, session)


def screens_until_ended(app, session):
    """The screens a page receives until it's told that its session ended."""
    screens = []
    for attempt in range(50):
        response = post(app, session)
        if response.status_code == 410:
            return screens
        assert response.status_code == 200, response.status
        response.decode_content()
        if "<pre" in response.text:
            screens.append(screen_text(response.text))
        time.sleep(0.1)
    raise AssertionError("the session didn't end")


def check_last_output():
    """A page receives what a command printed last, then hears that it ended."""

    def end_command(app):
        """A session whose command has ended before its page asked again."""
        session = new_app_session(app)
        app.multi.proc_write(session, "\r")
        assert wait_until(lambda: session in app.multi.last_screens), "not kept"
        return session

    with running_app("read x; echo last words") as app:
        session = end_command(app)
        screens = screens_until_ended(app, session)
        assert len(screens) == 1 and "last words" in screens[0], screens

        # A page that lost the reply gets the screen again when it asks for it.
        session = end_command(app)
        for attempt, extra in enumerate(({}, {"f": 1}, {"f": 1})):
            response = post(app, session, **extra)
            assert response.status_code == 200, (attempt, response.status)
            response.decode_content()
            assert "last words" in screen_text(response.text), attempt
        assert post(app, session).status_code == 410
        assert session not in app.multi.last_screens

        # The screen waits as long as a live session would, so less for a page that
        # has asked only once, and again each time it's sent.
        def time_left(session):
            proc = app.multi.last_screens[session]
            return proc["seen"] + app.multi.idle_limit(proc) - time.monotonic()

        def about(limit, session):
            return limit - 10 < time_left(session) <= limit

        def age(sessions, seconds):
            sessions[session]["seen"] -= seconds

        def expired(session):
            with app.multi.lock:
                app.multi.expire()
                return session not in app.multi.last_screens

        # A page that has asked once, and doesn't come back, is soon given up on.
        session = end_command(app)
        assert about(MINIMUM_TIMEOUT, session), time_left(session)
        age(app.multi.last_screens, MINIMUM_TIMEOUT + 1)
        assert expired(session)
        assert post(app, session).status_code == 410

        # Asking again, here after the end, shows that the page got its ID.
        session = end_command(app)
        post(app, session)
        assert about(DEFAULT_TIMEOUT, session), time_left(session)
        age(app.multi.last_screens, DEFAULT_TIMEOUT - 10)
        assert post(app, session, f=1).status_code == 200
        assert about(DEFAULT_TIMEOUT, session), time_left(session)
        age(app.multi.last_screens, DEFAULT_TIMEOUT + 1)
        assert expired(session)

        # The wait starts when the command ends, however long ago the page asked,
        # and input the command never read is dropped.
        session = new_app_session(app)
        post(app, session)
        with app.multi.lock:
            age(app.multi.proc, DEFAULT_TIMEOUT - 5)
            app.multi.proc[session]["input"] += b"never read"
            with mock.patch("os.read", return_value=b""):
                app.multi.proc_read(session)
        assert about(DEFAULT_TIMEOUT, session), time_left(session)
        assert not app.multi.last_screens[session]["input"]

        # If it can't be drawn, the page is told why the session ended.
        session = end_command(app)
        app.multi.last_screens[session]["term"].dumphtml = None
        with unittest.TestCase().assertLogs("achilterm.achilterm"):
            response = post(app, session)
        assert response.status_code == 410, response.status
        assert "server error" in response.text, response.text


def check_failed_start():
    """A command that can't be started says so, and leaves nothing running."""
    # an expected kind of error, and any other
    failure = FileNotFoundError(2, "No such file or directory")
    check_failed_exec(failure, "FileNotFoundError: [Errno 2] No such")
    check_failed_exec(AttributeError(), "AttributeError:")

    # A terminal that can't be set up is closed again.
    failure = OSError("deliberate failure")
    with running_app("sleep 30") as app:
        with (
            mock.patch("fcntl.ioctl", side_effect=failure),
            unittest.TestCase().assertRaisesRegex(OSError, "deliberate"),
        ):
            app.multi.create()
        assert not app.multi.proc, app.multi.proc
        assert wait_until(lambda: not app.multi.ending), app.multi.ending

        # So is one whose caller is interrupted as signals are let through again.
        real_sigmask, server_pid = signal.pthread_sigmask, os.getpid()

        def interrupt(how, mask):
            result = real_sigmask(how, mask)
            # only here, not in the new process, which sets its own mask
            if how == signal.SIG_SETMASK and os.getpid() == server_pid:
                raise KeyboardInterrupt
            return result

        before = real_sigmask(signal.SIG_BLOCK, [])
        with app.multi.lock:  # so that only create() can close the session
            with (
                mock.patch("signal.pthread_sigmask", interrupt),
                unittest.TestCase().assertRaises(KeyboardInterrupt),
            ):
                app.multi.create()
            assert not app.multi.proc, app.multi.proc
        assert real_sigmask(signal.SIG_BLOCK, []) == before
        assert wait_until(lambda: not app.multi.ending), app.multi.ending


def check_failed_exec(failure, reason):
    server_pid = os.getpid()
    with running_app("true") as app:
        with mock.patch("os.execvpe", side_effect=failure), app.multi.lock:
            session = new_app_session(app)
            # The new process must never get back here.
            assert os.getpid() == server_pid
        text = "".join(screens_until_ended(app, session))
        message = text.partition("couldn't start /bin/sh: ")[2]
        assert reason in message, text.strip()


def check_without_path():
    """Commands get a default PATH when the server has none."""
    command = 'echo "path=$PATH"; sleep 30'
    with mock.patch.dict(os.environ), running_app(command) as app:
        del os.environ["PATH"]
        send = functools.partial(app_screen, app, new_app_session(app))
        wait_for_screen(send, "", re.escape(f"path={os.defpath}"))


def check_announcement():
    """A server says where it is, and runs even if it can't."""
    # Asked for any free port, it names the one it got.
    server = subprocess.Popen(
        [COMMAND, "--port=0"], stdout=subprocess.PIPE, env=buffered_environment()
    )
    try:
        port = int(re.search("localhost:([0-9]+)/", announced_address(server))[1])
        assert port, "no port"
        assert "Achilterm" in fetch(f"http://127.0.0.1:{port}/")
    finally:
        terminate(server)

    # Its output goes nowhere, or isn't being read and has no room.
    reader, writer = os.pipe()
    os.close(reader)
    check_unwritten_announcement(writer)
    reader, writer = os.pipe()
    os.set_blocking(writer, False)
    with contextlib.suppress(BlockingIOError):
        while True:
            os.write(writer, b"x" * 4096)
    os.set_blocking(writer, True)
    try:
        check_unwritten_announcement(writer)
    finally:
        os.close(reader)


def check_unwritten_announcement(output):
    """A server whose standard output can't take its address still serves."""
    port = free_port()
    server = subprocess.Popen(
        [COMMAND, f"--port={port}"], stdout=output, stderr=subprocess.PIPE
    )
    os.close(output)
    try:
        assert wait_until(lambda: not connection_refused(port)), "not listening"
        assert "Achilterm" in fetch(f"http://127.0.0.1:{port}/")
    finally:
        errors = terminate(server)[1].decode()
    assert server.returncode == 0 and not errors, (server.returncode, errors)


def check_sizes():
    """A new terminal has the size asked for, or the usual one if it can't."""
    usual = (80, 25)
    for width, height, expected in (
        ("80", "25", usual),
        ("3", "3", (3, 3)),
        ("255", "99", (255, 99)),
        ("080", "09", (80, 9)),
        ("2", "30", usual),
        ("256", "30", usual),
        ("100", "2", usual),
        ("100", "100", usual),
        ("-80", "30", usual),
        ("abc", "30", usual),
        ("100", "", usual),
        ("8.5", "30", usual),
        ("9" * 5000, "30", usual),
        (None, "30", usual),
    ):
        size = requested_size({"w": width, "h": height})
        assert size == expected, (str(width)[:20], height, size)
    assert requested_size({}) == usual

    # The page's settings are checked the same way, and may have leading zeros.
    for options in (
        {"width": 256},
        {"height": 2},
        {"width": None},
        {"width": 80.5},
        {"height": float("inf")},
        {"width": "0x50"},
    ):
        with unittest.TestCase().assertRaisesRegex(ValueError, "must be a whole"):
            AchilTerm(**options)
    with running_app("sleep 30", width="0100", height="030") as app:
        page = webob.Request.blank("/").get_response(app).text
        assert '("term",100,30)' in page, page
        for fields, expected in (
            ({"w": 100, "h": 30}, (100, 30)),
            ({"w": "abc", "h": 30}, usual),
        ):
            response = post(app, **fields)
            assert response.status_code == 200, (fields, response.status)
            response.decode_content()
            rows = screen_text(response.text).splitlines()
            assert (len(rows[0]), len(rows)) == expected, (fields, len(rows))


def check_content_types():
    """Only a whole URL-encoded UTF-8 form, as the page sends, reaches a terminal."""
    form = "application/x-www-form-urlencoded"
    upload = webob.Request.blank("/u", POST={"s": "", "k": ("k", b"x")})
    plain = terminal_query()
    with running_app("sleep 30") as app:

        def status(content_type, body=plain, **environ):
            headers = {**PAGE_HEADERS, "Content-Type": content_type}
            request = webob.Request.blank(
                "/u", method="POST", body=body, headers=headers
            )
            request.environ.update(environ)
            return request.get_response(app).status_code

        for content_type, body in (
            (f"{form};charset=nonsense", plain),
            (f"{form};charset=latin-1", plain),
            ("multipart/form-data", plain),
            (upload.headers["Content-Type"], upload.body),
            ("text/plain", plain),
            ("", plain),
        ):
            assert status(content_type, body) == 415, content_type
        # Less arrives than was announced, as when a connection drops.
        arriving = {"wsgi.input": io.BytesIO(b"s="), "webob.is_body_seekable": False}
        assert status(form, CONTENT_LENGTH="1000", **arriving) == 408
        assert not app.multi.proc, "a refused request started a terminal"
        # The browser names the character set; other clients may not.
        for content_type in (form, f"{form};charset=UTF-8", f"{form}; charset=utf8"):
            assert status(content_type) == 200, content_type


def check_stalled_requests():
    """A silent request isn't waited for forever, but a slow reply isn't cut off."""
    head = (
        b"POST /u HTTP/1.1\r\nHost: localhost\r\nX-Achilterm: 1\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        b"Content-Length: 100\r\n"
    )
    page = b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
    limit = 0.2

    def exchange_bytes(server, sent, pause=0):
        """All that comes back, read after a pause."""
        with socket.create_connection(server.server_address, timeout=10) as link:
            link.sendall(sent)
            time.sleep(pause)
            with link.makefile("rb") as replies:
                return replies.read()

    with (
        mock.patch.object(RequestHandler, "timeout", limit),
        running_app("sleep 30") as app,
    ):
        # Far more than the connection holds while nothing is being read.
        app.files["index"] = "x" * 2**25
        for handler in (RequestHandler, NoLogWSGIRequestHandler):
            with (
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
                make_server(
                    "127.0.0.1",
                    0,
                    app,
                    server_class=ThreadingWSGIServer,
                    handler_class=handler,
                ) as server,
            ):
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    # Nothing at all, part of the headers, and part of the body.
                    for sent, expected in (
                        (b"", b""),
                        (head, b""),
                        (head + b"\r\ns=", b"HTTP/1.0 408 "),
                    ):
                        reply = exchange_bytes(server, sent)
                        assert reply[:13] == expected, (sent, reply)
                    reply = exchange_bytes(server, page, pause=limit * 3)
                    body = reply.partition(b"\r\n\r\n")[2]
                    assert len(body) == 2**25, len(body)
                finally:
                    server.shutdown()
            errors = stderr.getvalue()
            assert "Traceback" not in errors, errors
            # Requests are only logged if asked for.
            assert ("timed out" in errors) == (handler is RequestHandler), errors
        assert not app.multi.proc, "a stalled request started a terminal"


def check_many_descriptors():
    """Terminals must work when their descriptors are numbered 1024 or higher."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 2048 if hard == resource.RLIM_INFINITY else min(2048, hard)
    if wanted < 1100:
        print(f"skipping the descriptor check: the open file limit is {hard}")
        return
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
    filler = [os.open(os.devnull, os.O_RDONLY)]
    try:
        while filler[-1] < 1024:
            filler.append(os.open(os.devnull, os.O_RDONLY))
        with running_app('read line; echo "got $line"; sleep 30') as app:
            send = functools.partial(app_screen, app, new_app_session(app))
            wait_for_screen(send, "input\r", "got input")
            assert min(proc["fd"] for proc in app.multi.proc.values()) >= 1024
    finally:
        for fd in filler:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def screen_text(response):
    return "".join(ElementTree.fromstring(response.encode()).itertext())


def main():
    reported = subprocess.run(
        [COMMAND, "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert version("achilterm-ng") in reported, reported

    server, port, base = start_server()

    # the session ID of each terminal this test opens, by the name used here
    sessions = {}

    def terminal(name, keys=""):
        if name not in sessions:
            sessions[name] = new_session(f"{base}u")
        # POST, as the browser does, so that large input fits.
        response = fetch(f"{base}u", terminal_query(sessions[name], keys))
        # The browser can't update the screen from invalid XML.
        ElementTree.fromstring(response.encode())
        return response

    def expect(name, keys, pattern):
        send = functools.partial(terminal, name)
        return wait_for_screen(send, keys, pattern)

    def end_shell(name):
        """End a terminal's shell, and wait until its page is told."""
        keys = "\x15exit\r"
        for attempt in range(50):
            try:
                terminal(name, keys)
            except urllib.error.HTTPError as error:
                text = error.read().decode()
                # told that it ended, not that something went wrong with it
                assert error.code == 410 and "has ended. Reload" in text, text
                return
            keys = ""
            time.sleep(0.1)
        raise AssertionError(f"{name} didn't end")

    try:
        index = fetch(base)
        assert "github.com/paulschreiber/achilterm" in index, "no source link"
        for name in ("achilterm.js", "achilterm.css"):
            assert fetch(base + name), name

        # Keystrokes must not be accepted in an address, which may be logged.
        assert status(f"{base}u?s=get&w=80&h=25&c=1&k=x") == 405

        # A tab whose shell has ended must never reach a newer terminal. This
        # runs first, while the next terminal would reuse the ended one's file
        # descriptor.
        terminal("stale")
        end_shell("stale")
        terminal("newer")
        expect("newer", "echo private$((20+1))\r", "private21")
        query = terminal_query(sessions["stale"], "\x15echo intruder$((30+1))\r")
        code, text = refusal(f"{base}u", query)
        assert code == 410, (code, text)
        time.sleep(1)
        screen = terminal("newer", " ")
        assert "intruder31" not in screen_text(screen), screen_text(screen)
        end_shell("newer")

        # An idle connection must not block other requests.
        with socket.create_connection(("127.0.0.1", port)):
            terminal("first")
            terminal("second")

            # Ending one session must leave the others working.
            end_shell("first")
            expect("second", "echo marker$((40+2))\r", "marker42")
            terminal("third")

            # A large paste must arrive complete.
            terminal(
                "third", "stty raw -echo; echo ready; head -c $((50000*2)) | wc -c\r"
            )
            time.sleep(1)
            expect("third", "x" * 100000, "100000")

            # Split characters, invalid bytes and terminal queries in the output
            # must not stop screen updates for any session.
            terminal("fourth")
            expect(
                "fourth",
                "printf '\\303'; sleep 0.5; printf '\\251 \\377\\n'; echo ok$((5+5))\r",
                "ok10",
            )
            expect(
                "fourth",
                "stty raw -echo; printf '\\033[c'; head -c 5 | od -An -c; stty sane\r",
                r"\[\s+\?\s+6\s+c",
            )
            expect("second", "echo still$((6+6))\r", "still12")

            # Characters beyond Latin-1 must appear as written, in the usual
            # colors, and control characters must not break the response.
            terminal("fifth")
            text = "\u00e9\u20ac\u03bb\u0416\u2500\u4e2d\U0001f600"
            # C0 and C1 controls, DEL, a noncharacter, a direction override and
            # a line separator.
            controls = "\x01\x02\x0e\x7f\x85\ufffe\u202e\u2028"
            octal = "".join(f"\\{byte:03o}" for byte in (controls + text).encode())
            screen = expect(
                "fifth",
                f"printf '<{octal}>\\n'; echo end$((7+7))\r",
                "end14",
            )
            assert f"<{text}>" in screen_text(screen), screen
            colors = set(re.findall(r'class="f(\d+) b(\d+)"', screen))
            assert colors <= {("7", "0"), ("7", "1")}, colors
    finally:
        errors = terminate(server)[1].decode()

    assert "Traceback" not in errors, errors
    check_access()
    check_escape_sequences()
    check_expiry()
    check_unused()
    check_output_fault()
    check_processes()
    check_stop(signal.SIGINT)
    check_stop(signal.SIGHUP)
    # Only killing ends this command, which takes the server several seconds.
    check_stop(signal.SIGTERM, "HUP TERM")
    check_end_of_output()
    check_last_output()
    check_failed_start()
    check_without_path()
    check_sizes()
    check_content_types()
    check_stalled_requests()
    check_many_descriptors()
    check_announcement()
    print("smoke test passed")


if __name__ == "__main__":
    main()
