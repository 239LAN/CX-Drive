# CX-Drive

**[简体中文](README.md) | English**

CX-Drive is an out-of-the-box, self-hostable lightweight cloud drive system. Built with Flask, it supports file management, share links, recycle bin, membership plans, and an admin console. Deploy or upgrade on mainstream Linux servers with a single command.

## Features

- **File management**: folders, upload/download, search & sort, rename / move / copy, batch operations, recycle bin to prevent accidental deletion
- **Large file transfer**: chunked upload with MD5 instant-upload (server-side dedup), resumable-friendly
- **Share links**: share a single file or an entire folder; optional extraction password, expiry date, and download-count limits. Visitors can browse and download **without an account** (anonymous downloads throttled to 1 MiB/s)
- **Quotas & membership**: storage, per-file size limit, download throttle, and monthly traffic are all controlled per plan; optional traffic / storage add-ons
- **Admin console**: user management, plan / add-on management, balance adjustment, membership gifting, statistics dashboard
- **Security by design**: hashed passwords, brute-force lockout on share passwords (per IP + share), external links served only through the web page to prevent hot-linking
- **One-command deployment**: the standalone installer embeds the full source code; `sudo bash install.sh` performs a fresh install or an in-place upgrade

## Tech Stack

- Python 3 + Flask + SQLAlchemy + Flask-Login
- Bootstrap 5 (CDN) + Bootstrap Icons
- SQLite out of the box (switchable to MySQL / PostgreSQL)
- Production: Gunicorn + systemd (service name `cx-pan`)

## Project Layout

```
cx-drive/
├── app.py                  # Dev entry point (python app.py, port 5000)
├── wsgi.py                 # Production entry point (gunicorn wsgi:app)
├── config.py               # Application configuration
├── requirements.txt        # Python dependencies
├── gunicorn.conf.py        # Gunicorn config (default port 4280, override via .env)
├── manage.py               # CLI: create/revoke/list admins
├── app/
│   ├── __init__.py         # App factory (creates tables + seeds default plans)
│   ├── models.py           # Data models (users/files/shares/orders/ledger...)
│   ├── scheduler.py        # Scheduled jobs (trash cleanup, etc.)
│   ├── routes/             # Blueprints: auth / files / share / preview / billing / admin / main
│   ├── services/           # Business logic: files, quota, billing
│   ├── templates/          # Jinja2 templates
│   └── utils/              # Helpers & Jinja filters
├── build_install.py        # Builds the standalone installer (outputs install.sh)
├── install-template.sh     # Installer script template (used by the builder)
├── install.sh              # Standalone installer (build artifact, ready to deploy)
└── _smoke_run.py           # Regression smoke tests
```

## Local Development

Requires Python 3.10+.

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python app.py            # Open http://127.0.0.1:5000
```

> The Tsinghua PyPI mirror is used in the example above; feel free to omit it if it is not needed in your region.

## Deploy on Linux (systemd distributions)

The installer auto-detects the package manager and installs dependencies, covering:

| Distribution | Package manager | System packages |
| --- | --- | --- |
| Debian / Ubuntu | apt | python3 + venv + pip + rsync |
| RHEL / CentOS Stream / AlmaLinux / Rocky / Fedora | dnf (yum on legacy) | python3 + pip + rsync |
| Arch Linux | pacman | python + pip + rsync |
| openSUSE | zypper | python3 + pip + rsync |

> Alpine (apk / OpenRC) is not supported yet — the installer relies on systemd for service management. If you must use Alpine, run it manually via rc-service or a container.

### Option 1: Standalone installer (recommended)

Upload the build artifact `install.sh` to the server and run:

```bash
sudo bash install.sh
```

The script automatically: installs system dependencies → creates the run user → deploys code to `/opt/cx-pan` → generates the `.env` secret → creates a virtualenv and installs dependencies → registers and starts the `cx-pan` systemd service.

### Option 2: Install from the source tree

Place the source code anywhere on the server and run:

```bash
sudo bash install-template.sh            # When the script sits next to the source
sudo bash install-template.sh /path/to/source
```

### Upgrade in place

**Just run the same command again.** The database (`instance/`), user data (`storage/ uploads/`), and secrets (`.env`) are preserved automatically; only the code and dependencies are updated, then the service restarts.

### First run

1. Register an admin account on the web page.
2. Promote it to admin:

```bash
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py create-admin <your-username>
```

### Common operations

```bash
systemctl status cx-pan                  # Service status
journalctl -u cx-pan -f                  # Follow logs in real time
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py list-admin      # List admins
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py revoke-admin <username>  # Revoke admin
```

## Rebuilding the standalone installer

After changing the source code, regenerate the release installer:

```bash
python build_install.py
# Outputs install.sh with the latest source embedded
# (data / secrets / dev leftovers are excluded automatically)
```

## Smoke tests

```bash
python _smoke_run.py
```

Runs against an isolated temporary database and storage directory, covering 39 regression assertions across the user side / share links / admin console. All pass means the core functionality is healthy.

## Configuration

Tune via `/opt/cx-pan/.env` after deployment:

| Variable | Default | Description |
| --- | --- | --- |
| `SECRET_KEY` | Randomly generated at install | Session signing secret; keep it private |
| `CLOUDPAN_BIND` | `0.0.0.0:4280` | Listen address and port |
| `CLOUDPAN_WORKERS` | Auto (based on CPU count) | Number of Gunicorn workers |

## Notes

- Recharge / top-up is not included; integrate your own payment API if you need it.
- Frontend assets (Bootstrap) are loaded from a public CDN. For fully offline environments, download the static assets and update `app/templates/base.html`.
- Not recommended for production use.

## License

CX-Drive (创想云盘) © 2026 [239LAN](https://github.com/239LAN), released under the **GNU Affero General Public License v3.0**. See [LICENSE](LICENSE).

Under AGPL-3.0: anyone providing a network service based on this project (including derivatives deployed after modification) must make the complete corresponding source code available to users of that service under the same license.
