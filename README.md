# linux-server-setup-kit

![CI](https://github.com/ahmedbmohamed/linux-server-setup-kit/actions/workflows/ci.yml/badge.svg)

One Ansible command turns a **fresh Linux server** (RHEL / Rocky / AlmaLinux 9, Ubuntu 22.04 / 24.04, Debian 12) into a **production-ready web server**, with **automatic encrypted backups** and a **restore test that proves the backups work**.

Built by Ahmed Ben Mohamed, Red Hat Certified Engineer (RHCE).

| Role | What it sets up |
|---|---|
| [`app_server`](#what-gets-deployed) | Nginx reverse proxy with HTTPS (free Let's Encrypt certificate, or self-signed for labs), MariaDB with its own user, the app as a sandboxed systemd service, firewall ports |
| [`backup`](#backups-and-restore-test) | Nightly encrypted backups (restic) of files + database dumps to local disk, S3, Backblaze B2 or SFTP, with retention and integrity checks |
| [`restore_test.yml`](#backups-and-restore-test) | Restores the latest backup into a temporary area, compares files byte by byte and database rows with the live server, cleans up, writes a report |

**Production and staging** come from the same code: a separate staging copy (own database, ports and backups) can even run on the same server.

## What gets deployed

```
Internet ──► Nginx :443 (HTTPS, HTTP→HTTPS redirect, security headers, gzip)
               └─► Gunicorn 127.0.0.1:8000 (systemd, sandboxed user, auto-restart)
                     └─► MariaDB 127.0.0.1:3306 (app user, own database)
Nightly ──► restic backup (DB dump + /etc + /opt + /var/www) ──► local / S3 / B2 / SFTP
```

| Part | Details |
|---|---|
| HTTPS | Let's Encrypt when a domain + email are set (automatic renewal, Nginx reloaded after renewal); self-signed certificate otherwise. TLS 1.2/1.3, HSTS, `server_tokens off`, X-Frame-Options, X-Content-Type-Options, Referrer-Policy |
| App | Included demo Flask app (page + `/health` with a database check), or **your own app from a git repo** (`app_server_git_repo`). Python virtualenv, Gunicorn |
| systemd sandbox | Runs as its own no-login user; code owned by root (read-only for the app); `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`, no capabilities |
| Secrets | Database and backup passwords generated once and stored in `credentials/` on your machine (git-ignored); `.env` readable by root and the app only |
| Database | MariaDB bound to 127.0.0.1, no anonymous users, no test database; app user can only use its own database |
| Network | Only Nginx is public. The app and the database listen on 127.0.0.1. Opens 80/443 if UFW or firewalld is active |
| SELinux | Stays enforcing on RHEL; only `httpd_can_network_connect` is turned on so Nginx can reach the app |
| Check | At the end, the playbook calls `/health` through Nginx over HTTPS and fails if the app or database does not answer |

## Backups and restore test

- **What:** database dump (`mysqldump --single-transaction`, made just before each backup), `/etc`, `/opt`, `/var/www` (virtualenvs and caches excluded).
- **Where:** encrypted [restic](https://restic.net) repository on local disk, S3, Backblaze B2 or SFTP.
- **When:** every night (systemd timer, catches up after downtime), plus once at deployment.
- **How long:** 7 daily, 4 weekly, 6 monthly by default, then `restic check` verifies the repository.
- **Status:** `/var/backups/last-backup-<app>.json` (ok/failed, snapshot, duration).

`playbooks/restore_test.yml` makes a fresh backup, then **restores it and checks it**, without touching live data:

1. restores the latest snapshot into `/var/tmp/restore-test-<app>`
2. compares the restored `/etc/nginx` and `/opt` **byte by byte** with the live server
3. loads each database dump into a **temporary database** and compares tables and row counts with the live one
4. removes the temporary folder and databases
5. writes `reports/<host>-<app>-restore-test.md` and **fails** if anything does not match

### Results (lab test, Ubuntu 24.04)

| Check | Result |
|---|---|
| HTTP → HTTPS redirect | 301 |
| `/health` through Nginx over HTTPS | `{"status":"ok","database":"ok","env":"production"}` |
| Ports open to the network | 443 (and 80 for the redirect) only; app and MariaDB on 127.0.0.1 |
| First backup | `BACKUP OK` |
| Restore test | **PASSED**: snapshot restored in 19.6 s, **108,859 files identical**, database restored with the same tables and rows |
| Second run of the playbook | `changed=0` (idempotent) |

## Quick start

```bash
pip install ansible-core
cp inventory/hosts.example.ini inventory/hosts.ini      # your server(s)
nano inventory/group_vars/all.yml                       # app name, domain + email for Let's Encrypt

ansible-playbook playbooks/site.yml -K                  # deploy app + backups (production and staging)
ansible-playbook playbooks/site.yml -l staging -K       # only staging
ansible-playbook playbooks/restore_test.yml -K          # prove the backups can be restored
ansible-playbook playbooks/backup_now.yml -K            # extra backup before a risky change
```

Then open `https://<server-ip>/` (self-signed) or `https://<your-domain>/`.

> **Keep `credentials/` safe** (password manager, encrypted drive). The restic password in it is the only way to read the backups.

### Backups to the cloud

```yaml
# inventory/group_vars/all.yml
backup_repository: "b2:my-bucket:web1"           # or s3:s3.amazonaws.com/my-bucket/web1
backup_env:
  B2_ACCOUNT_ID: "..."                            # use ansible-vault for real keys
  B2_ACCOUNT_KEY: "..."
```

### Your own app

```yaml
app_server_name: shop
app_server_git_repo: https://github.com/you/shop.git
app_server_wsgi_module: "wsgi:app"                # needs requirements.txt (with gunicorn)
app_server_extra_env:
  SECRET_KEY: "{{ vault_shop_secret_key }}"
```

## Main variables

| Variable | Default | Meaning |
|---|---|---|
| `app_server_name` | `demoapp` | Name of the app, user, service, database and backup set |
| `app_server_domain` / `app_server_letsencrypt_email` | `""` | Both set = Let's Encrypt; empty = self-signed |
| `app_server_http_port` / `app_server_https_port` | `80` / `443` | Staging uses 8080 / 8443 |
| `app_server_git_repo` | `""` | Deploy your app instead of the demo |
| `backup_repository` | `/var/backups/restic` | Local path, `s3:...`, `b2:...`, `sftp:...` |
| `backup_schedule` | `*-*-* 02:30:00` | systemd calendar format |
| `backup_keep_daily` / `weekly` / `monthly` | `7` / `4` / `6` | Retention |

All options: [`app_server`](roles/app_server/defaults/main.yml), [`backup`](roles/backup/defaults/main.yml).

## Pairs well with

[linux-hardening-ansible](https://github.com/ahmedbmohamed/linux-hardening-ansible): SSH, firewall, Fail2ban and automatic updates, plus a read-only security scan with a client report.

## CI

On every push, GitHub Actions: `ansible-lint` (production profile), then on a real Ubuntu 24.04 machine deploys **production and staging side by side**, checks both over HTTPS, checks nothing but Nginx is public, re-runs for **idempotence**, and runs the **restore test**.

## License

MIT
