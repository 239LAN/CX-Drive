# ChuangXiang Drive

**English | [简体中文](README.md)**

Chuangxiang Cloud Drive is an out-of-the-box, self-hostable lightweight cloud drive system. Built on Flask, it supports file management, share links, a recycle bin, membership plans, and an admin panel. A single command can complete deployment or in-place updates on mainstream Linux servers.

## Features

- **File management**: Folder directories, upload/download, search and sorting, rename / move / copy, batch operations, and a recycle bin to prevent accidental deletion
- **Large file transfer**: Chunked upload + MD5 instant upload, resume-friendly
- **Share links**: Share individual files or entire directories; supports access passwords, expiration dates, and download limits; visitors can browse and download without logging in (guest downloads are throttled to 1 MiB/s)
- **Quotas and memberships**: Storage capacity, max file size, download speed limit, and monthly traffic can all be controlled by plan; add-on storage / traffic packages are supported
- **Admin panel**: User management, plan / add-on package management, balance adjustments, membership grants, and a statistics dashboard
- **Security design**: Hashed password storage, share password brute-force protection (lockout after too many failed attempts), and share links limited to web downloads to prevent direct-link abuse
- **One-command deployment**: The single-file installer embeds the complete source code; on Linux, `sudo bash install.sh` completes a fresh install or in-place update

## Tech Stack

- Python 3 + Flask + SQLAlchemy + Flask-Login
- Bootstrap 5 (CDN) + Bootstrap Icons
- SQLite (works out of the box; can be switched to MySQL / PostgreSQL)
- Production deployment: Gunicorn + systemd (managed service name `cx-pan`)

## Directory Structure

```
Chuangxiang Cloud Drive/
├── app.py                  # Development entry point (python app.py, port 5000)
├── wsgi.py                 # Production entry point (gunicorn wsgi:app)
├── config.py               # Application configuration
├── requirements.txt        # Python dependencies
├── gunicorn.conf.py        # Gunicorn configuration (default port 4280, overridable via .env)
├── manage.py               # CLI: add/remove/query administrators
├── app/
│   ├── __init__.py         # Application factory (create tables + initialize default plans)
│   ├── models.py           # Data models (users/files/shares/orders/transactions...)
│   ├── scheduler.py        # Scheduled tasks (recycle bin cleanup, etc.)
│   ├── routes/             # Routes: auth / files / share / preview / billing / admin / main
│   ├── services/           # Business services: files, quotas, billing
│   ├── templates/          # Jinja2 templates
│   └── utils/              # Utility functions and Jinja filters
├── build_install.py        # Build single-file installer (outputs install.sh)
├── install-template.sh     # Installer script template (used by builder)
├── install.sh              # Single-file installer (build artifact, ready to deploy)
└── _smoke_run.py           # Regression smoke tests
```

## Local Development

Requires Python 3.10+.

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python app.py            # Open http://127.0.0.1:5000 in your browser
```

## Deploy to Linux (systemd Distributions)

Automatically detects the package manager and installs dependencies. Supports the following mainstream distributions:

| Distribution | Package manager | System dependencies |
| --- | --- | --- |
| Debian / Ubuntu | apt | python3 + venv + pip + rsync |
| RHEL / CentOS Stream / AlmaLinux / Rocky / Fedora | dnf (yum on older versions) | python3 + pip + rsync |
| Arch Linux | pacman | python + pip + rsync |
| openSUSE | zypper | python3 + pip + rsync |

> Alpine (apk / OpenRC) is not supported yet — the script relies on systemd to manage the service; if you must use it, manually switch to rc-service or use a container.

### Method 1: Single-file installer (recommended)

Upload the build artifact `install.sh` to the server and run:

```bash
sudo bash install.sh
```

The script automatically: installs system dependencies → creates a runtime user → deploys code to `/opt/cx-pan` → generates secrets in `.env` → creates a virtual environment and installs dependencies → registers and starts the systemd service `cx-pan`.

### Method 2: Install directly from source directory

Place the project source in any directory on the server and run:

```bash
sudo bash install-template.sh            # script and source in the same directory
sudo bash install-template.sh /path/to/source
```

### In-place Update

**Run the same command again**. The database (`instance/`), user data (`storage/ uploads/`), and secrets (`.env`) are automatically preserved; only code and dependencies are updated, and the service is restarted.

### First Use

1. Register an admin account on the page
2. Promote it to administrator:

```bash
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py create-admin <your-username>
```

### Common Operations Commands

```bash
systemctl status cx-pan                  # Check service status
journalctl -u cx-pan -f                  # Follow logs in real time
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py list-admin      # List admins
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py revoke-admin <username>  # Revoke admin
```

## Rebuilding the Single-file Installer

After modifying the source, if you need to update the release installer:

```bash
python build_install.py
# Outputs install.sh; the payload includes the latest source (data/secrets/development leftovers are automatically excluded)
```

## Smoke Tests

```bash
python _smoke_run.py
```

Uses an isolated temporary database and storage directory, covering 39 regression assertions across the user side / share links / admin side. If all pass, basic functionality is healthy.

## Configuration

After deployment, adjust via `/opt/cx-pan/.env`:

| Variable | Default | Description |
| --- | --- | --- |
| `SECRET_KEY` | Randomly generated at install time | Session signing key; do not disclose |
| `CLOUDPAN_BIND` | `0.0.0.0:4280` | Listen address and port |
| `CLOUDPAN_WORKERS` | Automatically calculated based on CPU | Number of Gunicorn processes |

## Notes

- Recharge/top-up functionality must be integrated with a payment API yourself.
- The frontend dependency (Bootstrap) is loaded via public CDN; for a fully intranet environment, download the static assets yourself and modify `app/templates/base.html`.
- Not recommended for production use.

## License

Chuangxiang Cloud Drive © 2026 [239LAN](https://github.com/239LAN), open-sourced under the **GNU Affero General Public License v3.0**; see [LICENSE](LICENSE).

Under AGPL-3.0: Anyone providing a network service based on this project (including secondary development or modified deployment) must also make the complete source code available to service users under the same license.
