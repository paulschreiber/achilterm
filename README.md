Achilterm
=========

**Achilterm** is a lightweight **UTF-8** web based terminal.

![Achilterm](https://raw.githubusercontent.com/paulschreiber/achilterm/main/img/achilterm.png)

Achilterm is written in Python (and some AJAX javascript for client
side).

Achilterm is **very simple to install** on Linux, MacOS X, FreeBSD,
Solaris, cygwin and any Unix that runs Python.

Achilterm is initially forked from Ajaxterm which was inspired by
Anyterm.

Achilterm was developed by Florent Gallaire <f@gallai.re>

Original website: <https://f.gallai.re/achilterm>

About achilterm-ng
------------------

``achilterm-ng`` is a modified version of Achilterm, maintained by Paul
Schreiber since November 2026. It installs the same ``achilterm`` command and
Python module, so don't install it alongside the original ``achilterm``
package. See the [changelog](#changelog) for what's different.

Source and bug reports: <https://github.com/paulschreiber/achilterm>

Download and Install
--------------------

To install the latest release from PyPI into its own virtual environment:

    $ sudo python3 -m venv /opt/achilterm
    $ sudo /opt/achilterm/bin/pip install achilterm-ng
    $ sudo ln -s /opt/achilterm/bin/achilterm /usr/local/bin/achilterm

Development
-----------

Development uses [uv](https://docs.astral.sh/uv/). To get the source and set
up an environment with the development tools:

    $ git clone https://github.com/paulschreiber/achilterm.git
    $ cd achilterm
    $ uv sync

To run the development version:

    $ uv run achilterm

To lint the code and check its formatting:

    $ uv run ruff check .
    $ uv run ruff format --check .

To run the same checks, plus file cleanup, automatically before each commit:

    $ uv run pre-commit install

After changing dependencies in ``pyproject.toml``, update ``uv.lock`` with
``uv lock`` and commit both files.

The version number is set in ``pyproject.toml``. To change it, run
``uv version`` with the new number, which also updates ``uv.lock``, and
update the man page and changelog.

Running
-------

To run Achilterm after installation:

    $ achilterm

To run Achilterm from the source without installation:

    $ ./achilterm/achilterm.py

Then point your browser to this URL: ``http://localhost:8022/``

Achilterm runs in the foreground. To keep it running, use systemd (below) or
your system's service manager. For a temporary server, ``nohup`` works on most
systems, though some still stop it when you log out:

    $ nohup achilterm &

Run it as an unprivileged user rather than root, unless you want it to use
``/bin/login``.

Run with systemd
----------------

``achilterm.service`` runs Achilterm as an unprivileged ``achilterm`` user.
Users log in over SSH to ``localhost``, so an SSH server with password
authentication must be running.

Create the user, install the unit and start it:

    $ sudo useradd --system --user-group --create-home --shell /usr/sbin/nologin achilterm
    $ sudo cp achilterm.service /etc/systemd/system/
    $ sudo systemctl daemon-reload
    $ sudo systemctl enable --now achilterm

The unit expects ``/usr/local/bin/achilterm``. If yours is elsewhere, change
``ExecStart``. To see errors:

    $ sudo journalctl -u achilterm

Achilterm listens on ``localhost`` and serves plain HTTP. For remote access,
use an SSH tunnel or an HTTPS reverse proxy with access control.

Documentation and Caveats
-------------------------

-  Achilterm supports Python 3.11 and above

-  Achilterm requires WebOb 1.8.11 or later

-  If run as root achilterm will run ``/bin/login``, otherwise it will run
   ``ssh localhost``. To use an other command use the ``-c`` option.

-  By default Achilterm only listen at ``127.0.0.1:8022``. For remote
   access, it is strongly recommended to use **https SSL/TLS**, and that
   is simple to configure if you use the apache web server using
   ``mod_proxy``.

   Using ssl will also speed up achilterm (probably because of keepalive).

Commandline usage
-----------------

    usage: achilterm [options]

    options:
      --version                  show program's version number and exit
      -h, --help                 show this help message and exit
      -p PORT, --port=PORT       set the TCP port (default: 8022)
      -c CMD, --command=CMD      set the command (default: /bin/login or ssh localhost)
      -l, --log                  log requests to stderr (default: quiet mode)
      -i INDEX_FILE, --index=INDEX_FILE
                                 default index file (default: achilterm.html)
      -w WIDTH, --width=WIDTH    set the width, from 3 to 255 (default: 80)
      -H HEIGHT, --height=HEIGHT set the height, from 3 to 99 (default: 25)
      --timeout=TIMEOUT          close a terminal not shown by a browser for this
                                 many seconds (default: 300, at least 90)
      --allow-host=NAME          also accept requests for this host name; repeat
                                 for more (default: only localhost)

Apache configuration example
----------------------------

Anyone who can reach Achilterm can try to log in, so put it behind HTTPS and
consider restricting who can reach it. This Apache 2.4 example serves it at
``https://terminal.example.com/achilterm/``.

Enable the modules it needs (on Debian or Ubuntu):

    $ sudo a2enmod ssl proxy proxy_http

Achilterm only answers requests for ``localhost``, which is what Apache sends
in this example. If your proxy passes on the public name instead, as with
``ProxyPreserveHost On``, start Achilterm with
``--allow-host=terminal.example.com``.

Add this virtual host, using your own server name and certificate:

    <VirtualHost *:443>
        ServerName terminal.example.com

        SSLEngine on
        SSLCertificateFile /etc/ssl/certs/terminal.example.com.pem
        SSLCertificateKeyFile /etc/ssl/private/terminal.example.com.key

        RedirectMatch permanent "^/achilterm$" "/achilterm/"

        <Location "/achilterm/">
            Require all granted

            ProxyPass "http://localhost:8022/"
            ProxyPassReverse "http://localhost:8022/"
        </Location>
    </VirtualHost>

Then check the configuration and reload Apache:

    $ sudo apache2ctl configtest
    $ sudo systemctl reload apache2

To limit access to known networks, replace ``Require all granted`` with:

    Require ip 192.0.2.0/24

Logins made through Achilterm reach SSH from ``localhost``, so tools that
block addresses after failed logins, such as fail2ban, normally ignore them.
Use strong passwords on every account that can log in over SSH.

Changelog
---------

### 0.30 (November 2026)

-  Publish as ``achilterm-ng``

-  Remove daemon mode and its ``-d``/``--daemon``, ``-P``/``--pidfile`` and
   ``-u``/``--uid`` options; run Achilterm under a service manager such as
   systemd, which also chooses the user it runs as

-  Remove Achiltermlite, the stripped-down page, and its ``-L``/``--lite``
   option; a custom ``--index`` page based on it must use ``achilterm.js`` and
   ``achilterm.css`` instead

-  Serve requests on threads, so an idle connection no longer blocks others

-  Fix a crash and a held lock when a session ends, which made other
   requests hang

-  Don't close an idle session while the browser is still polling it

-  Keep running if a session closes while Achilterm is waiting for output

-  Fix an error serving the terminal's JavaScript on Python 3.11

-  Fix installation with pip, which failed when WebOb wasn't already present

-  Fix the Paste button, which only worked in old browsers

-  Fix special keys, such as Backspace, the arrows and Ctrl-C, in current
   browsers, and remove support for Internet Explorer

-  Keep Alt with the arrows or Home from leaving the page, by sending the key to
   the terminal; Alt with Backspace deletes a word in shells such as bash and
   zsh

-  Ask before leaving or reloading a page whose terminal may still be running,
   since that loses the terminal

-  Send typing and show its output promptly, rather than sometimes after up to
   two seconds

-  Reject a ``--width`` or ``--height`` that can't be used, instead of ignoring
   it or serving a blank page, and fix a server error for a request with a size
   that isn't a number; sizes are now read as decimal, so forms such as ``0x50``
   are rejected and ``0120`` means 120

-  Reject a ``--port`` or ``--timeout`` that can't be used with a usage error,
   instead of showing a traceback or later stopping terminal output; a timeout
   is read as decimal, so ``0300`` means 300 rather than 192

-  Accept only URL-encoded UTF-8 forms, as the page sends, in terminal
   requests

-  Refuse unreadable or cut-off terminal requests instead of failing with a
   server error

-  Fix large input, such as a long paste, being cut off

-  Fix escape sequences that could stop or stall every terminal, move the
   cursor off the screen or change the screen's size, including the common
   "reset scroll region"

-  Fix terminals stopping when the server has more than about a thousand files
   and connections open

-  Clean up the processes of closed terminals, including when Achilterm is
   stopped: ask everything in a command's process group to end, and kill what
   remains after five seconds; a job meant to outlive its terminal needs
   ``setsid``

-  Start commands with the usual handling of broken pipes and other signals

-  Exit, rather than hang, when the port is already in use

-  When a command can't be started, say why in the terminal and end its new
   process, which used to carry on as a second copy of the server; start
   commands even if ``PATH`` isn't set

-  Show the last of a command's output before saying that its session ended

-  Notice on macOS and BSD that a command has ended, instead of leaving its
   terminal open

-  Have the server issue random 128-bit session IDs, and refuse requests from
   other websites or for unknown host names (see ``--allow-host``)

-  When a session ends, for example after ``exit``, say so instead of starting
   a new login; reload the page to start another

-  Close terminals whose page is no longer open (see ``--timeout``)

-  Don't let other websites show the terminal in a frame

-  Retry after a lost connection, ignoring typing until it returns (though
   Ctrl-Shift-R still reloads the page, as Command-R does at any time on a Mac),
   and show why a page was refused

-  Fix a security flaw that could connect a tab whose shell had ended to
   another user's terminal

-  Fix characters beyond Latin-1 showing as other characters, and ignore
   control characters that could stop the screen updating or reorder it; wide
   and combining characters can still shift the rest of a line

-  Fix output that stopped screen updates for every session, such as a split
   or invalid character, or a program asking the terminal to identify itself

-  Remove the GET option, which could record keystrokes in server logs;
   keystrokes are now accepted only in POST requests

-  Use ``html.escape()``, as ``cgi.escape()`` was removed from Python

-  Drop Python 2 support; require Python 3.11 or later and WebOb 1.8.11 or
   later

-  Replace ``setup.py`` with ``pyproject.toml``

-  Add a systemd service unit

-  Link to the source code from the terminal page

-  Update the Apache example for Apache 2.4

-  Document the missing options in the man page and fix its layout

-  Move the project to GitHub

Old versions
------------

-  Achilterm 0.13 require Python 2.5 and above

Older Achilterm versions only support latin1, if you use Ubuntu or any
``LANG==en_US.UTF-8`` distribution don't forget to ``$ unset LANG``:

-  Achilterm 0.12 require WebOb >= 1.2 (use it with Python 2.6 and 2.7)

-  Achilterm 0.11 require WebOb < 1.0 (use it with Python 2.5)

Compared to anyterm
-------------------

-  There are no partial updates, achilterm updates either all the screen
   or nothing. That make the code simpler and I also think it's faster.
   HTTP replies are always gzencoded. When used in 80x25 mode, almost
   all of them are below the 1500 bytes (size of an ethernet frame) and
   we just replace the screen with the reply (no javascript string
   handling).

-  Achilterm polls the server for updates with an exponentially growing
   timeout when the screen hasn't changed. The timeout is also resetted
   as soon as a key is pressed. Anyterm blocks on a pending request and
   use a parallel connection for keypresses. The anyterm approch is
   better when there aren't any keypress.

License
-------

Achilterm files are released under the GNU AGPLv3 or above license.

Achilterm codebase from Ajaxterm by Antony Lesuisse (email: al AT
udev.org), License Public Domain.
