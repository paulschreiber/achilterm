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

To install the development version from GitHub:

    $ git clone https://github.com/paulschreiber/achilterm.git
    $ cd achilterm
    $ python3 -m venv .venv
    $ .venv/bin/pip install .

To run Achilterm after installation:

    $ achilterm

To run Achilterm from the source without installation:

    $ ./achilterm/achilterm.py

Then point your browser to this URL: ``http://localhost:8022/``

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

-  Achilterm requires WebOb 1.8.9 or later

-  If run as root achilterm will run ``/bin/login``, otherwise it will run
   ``ssh localhost``. To use an other command use the ``-c`` option.

-  By default Achilterm only listen at ``127.0.0.1:8022``. For remote
   access, it is strongly recommended to use **https SSL/TLS**, and that
   is simple to configure if you use the apache web server using
   ``mod_proxy``.

   Using ssl will also speed up achilterm (probably because of keepalive).

-  Using GET HTTP request seems to speed up achilterm, just click on GET
   in the interface, but be warned that your keystrokes might be loggued
   (by apache or any proxy). I usually enable it after the login.

Achiltermlite
-------------

Achiltermlite is a stripped-down client-side version of Achilterm.

![Achiltermlite](https://raw.githubusercontent.com/paulschreiber/achilterm/main/img/achiltermlite.png)

Commandline usage
-----------------

    usage: achilterm [options]

    options:
      --version                  show program's version number and exit
      -h, --help                 show this help message and exit
      -p PORT, --port=PORT       set the TCP port (default: 8022)
      -c CMD, --command=CMD      set the command (default: /bin/login or ssh localhost)
      -l, --log                  log requests to stderr (default: quiet mode)
      -d, --daemon               run as daemon in the background
      -P PIDFILE, --pidfile=PIDFILE
                                 set the pidfile (default: /var/run/achilterm.pid)
      -i INDEX_FILE, --index=INDEX_FILE
                                 default index file (default: achilterm.html)
      -u UID, --uid=UID          set the daemon's user id
      -L, --lite                 use Achiltermlite
      -w WIDTH, --width=WIDTH    set the width (default: 80)
      -H HEIGHT, --height=HEIGHT set the height (default: 25)

Configuration example
---------------------

        Listen 443
        NameVirtualHost *:443

        <VirtualHost *:443>
           ServerName localhost
           SSLEngine On
           SSLCertificateKeyFile ssl/apache.pem
           SSLCertificateFile ssl/apache.pem

           ProxyRequests Off
           <Proxy *>
                   Order deny,allow
                   Allow from all
           </Proxy>
           ProxyPass /achilterm/ http://localhost:8022/
           ProxyPassReverse /achilterm/ http://localhost:8022/
        </VirtualHost>

Changelog
---------

### 0.30 (November 2026)

-  Publish as ``achilterm-ng``

-  Serve requests on threads, so an idle connection no longer blocks others

-  Fix a crash and a held lock when a session ends, which made other
   requests hang

-  Don't close an idle session while the browser is still polling it

-  Keep running if a session closes while Achilterm is waiting for output

-  Fix an error serving the terminal's JavaScript on Python 3.11

-  Fix installation with pip, which failed when WebOb wasn't already present

-  Use ``html.escape()``, as ``cgi.escape()`` was removed from Python

-  Drop Python 2 support; require Python 3.11 or later and WebOb 1.8.9 or
   later

-  Replace ``setup.py`` with ``pyproject.toml``

-  Add a systemd service unit

-  Link to the source code from the terminal page

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
