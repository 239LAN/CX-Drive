# CX-Drive（创想云盘）

**[简体中文](README.md) | English**

CX-Drive is an out-of-the-box, self-hostable lightweight cloud drive system. Built with Flask, it supports file management, in-browser preview, zip / unzip, share links, remote download, a recycle bin, membership plans, and an admin console. Deploy or upgrade on mainstream Linux servers with a single command.

## Features

- **File management**: folders, upload/download, search & sort, rename / move / copy, batch operations, recycle bin to prevent accidental deletion
- **In-browser preview**: images, text, audio and video open right in the web page (toggle via `config.yml`, members only)
- **Zip & unzip**: pack selected files / folders into a zip in one click, and unzip archives directly in the browser; multi-select downloads are zipped automatically
- **Large file transfer**: chunked upload with MD5 instant-upload (server-side dedup), resumable-friendly
- **Share links**: share a single file or an entire folder; optional extraction password, expiry date, and download-count limits. Visitors can browse and download **without an account** (anonymous downloads throttled to 1 MiB/s), and can save a share straight into their own drive
- **Remote download**: paste a URL and let the server fetch the file in the background (toggle via `config.yml`)
- **Quotas & membership**: storage, per-file size limit, download throttle, and monthly traffic are all controlled per plan; optional traffic / storage add-ons; signed-in users are unlimited by default
- **Admin console**: user management, plan / add-on management, balance adjustment, membership gifting, statistics dashboard
- **Security by design**: hashed passwords, upload extension allow-list plus magic-byte sniffing (rejects executables and scripts disguised as common formats), brute-force lockout on share passwords (per IP + share), external links served only through the web page to prevent hot-linking
- **One-command deployment**: the standalone installer embeds the full source code; `sudo bash CXDrive-release-<version>.sh` performs a fresh install or an in-place upgrade
- **Auto update**: checks GitHub Releases daily at 00:00 and installs new versions in place (disable in `config.yml`; falls back to a public proxy when GitHub is unreachable); the footer shows the current version and update hints

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
│   ├── scheduler.py        # Scheduled jobs (monthly traffic reset, daily update check, etc.)
│   ├── routes/             # Blueprints: auth / files / share / preview / billing / admin / main
│   ├── services/           # Business logic: files, quota, billing, remote download, auto-update
│   ├── templates/          # Jinja2 templates
│   └── utils/              # Helpers & Jinja filters
├── build_install.py        # Builds the standalone installer (outputs to dist/)
├── install-template.sh     # Installer script template (used by the builder)
├── VERSION                 # Version number (footer + auto-update comparison)
└── dist/                   # Build artifact: CXDrive-release-<version>.sh
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

Upload the build artifact `CXDrive-release-<version>.sh` to the server and run:

```bash
sudo bash CXDrive-release-<version>.sh
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

### Auto-update

Every day at 00:00 (server local time) the service asks the GitHub Releases API for the latest version:

1. When a newer version exists it downloads the release script `CXDrive-release-<version>.sh` (same name as the Release asset) to `/opt/cx-pan/instance/update/current.sh` and verifies its sha256;
2. It then runs it as root in a separate systemd unit through the sudoers rule written by the installer (`/etc/sudoers.d/cx-pan-update`, allowing only `/usr/local/sbin/cx-pan-auto-update`), so restarting the service does not interrupt the upgrade;
3. The database, user data, `.env` and `config.yml` are preserved; the update log is `/opt/cx-pan/instance/update/update.log`.

If `github.com` / `api.github.com` is unreachable (common on servers inside China), it automatically
falls back to the public GH proxy `https://v4.gh-proxy.org/` by prefixing the original URL
(`https://v4.gh-proxy.org/https://github.com/.../CXDrive-release-<version>.sh`). It always tries the
direct connection first and only then the proxy; if the proxy also fails the error is recorded and the
previous update status is kept.

Adjust it in `config.yml`:

```yaml
update:
  enabled: true                          # false = never install automatically, still checks daily and hints in the footer
  repo: 239LAN/CX-Drive                  # Update source (GitHub owner/name)
  proxy: https://v4.gh-proxy.org/        # Fallback proxy used only when the direct connection fails; empty disables it
```

## Rebuilding the standalone installer

After changing the source code, regenerate the release installer:

```bash
python build_install.py
# Reads VERSION and outputs dist/CXDrive-release-<version>.sh
# This single file is both the installer for the server and the GitHub Release asset
# (auto-update only recognizes this exact naming pattern)
```

Bump [VERSION](VERSION) before publishing a new release; the footer and the auto-update check both use it.

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
