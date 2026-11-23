#!/usr/bin/env python3
"""Restore test: restores the latest restic snapshot into a temporary folder,
compares files with the live server, loads every database dump into a
temporary database and compares row counts, then cleans up.

Prints one JSON document. Changes nothing on the live data.
Usage: restore_test.py --verify /etc/nginx /opt [--dump-dir /var/backups/db] [--backup-first]
"""
import argparse
import filecmp
import fnmatch
import gzip
import json
import os
import shutil
import subprocess
import time

IGNORE = ["venv", "__pycache__", "*.pyc", ".env.swp"]


def env(name):
    e = dict(os.environ)
    with open("/etc/restic/%s/backup.env" % name) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k] = v.strip().strip('"').strip("'")
    return e


def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kw)


def ignored(name):
    return any(fnmatch.fnmatch(name, p) for p in IGNORE)


def compare_tree(live, restored, out):
    if not os.path.isdir(live):
        return
    if not os.path.isdir(restored):
        out["missing"].append(live)
        return
    cmp = filecmp.dircmp(live, restored, ignore=[])
    for name in cmp.left_only:
        if not ignored(name):
            out["missing"].append(os.path.join(live, name))
    for name in cmp.common_files:
        if ignored(name):
            continue
        a, b = os.path.join(live, name), os.path.join(restored, name)
        if os.path.islink(a) or not os.path.isfile(a):
            continue
        out["compared"] += 1
        if not filecmp.cmp(a, b, shallow=False):
            out["different"].append(a)
    for name in cmp.common_dirs:
        if not ignored(name):
            compare_tree(os.path.join(live, name), os.path.join(restored, name), out)


def mysql(sql, db=None):
    cmd = ["mysql", "-NB"] + ([db] if db else []) + ["-e", sql]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def row_counts(db):
    counts = {}
    for t in mysql("SHOW TABLES", db).split():
        counts[t] = int(mysql("SELECT COUNT(*) FROM `%s`" % t, db).strip())
    return counts


def check_dump(path):
    db = os.path.basename(path)[:-len(".sql.gz")]
    tmp = "restoretest_%s" % db[:40]
    res = {"database": db, "dump": path, "ok": False}
    try:
        mysql("DROP DATABASE IF EXISTS `%s`; CREATE DATABASE `%s`" % (tmp, tmp))
        with gzip.open(path, "rt") as fh:
            r = run(["mysql", tmp], input=fh.read())
        if r.returncode != 0:
            res["error"] = r.stderr.strip()[:300]
            return res
        restored = row_counts(tmp)
        live = row_counts(db) if db in mysql("SHOW DATABASES").split() else {}
        res["tables"] = len(restored)
        res["restored_rows"] = sum(restored.values())
        res["live_rows"] = sum(live.values())
        res["same_tables"] = set(restored) == set(live)
        # Rows written after the backup are normal; fewer live rows than restored is not.
        res["ok"] = res["same_tables"] and all(restored[t] <= live.get(t, 0) for t in restored)
        res["detail"] = {t: {"restored": restored[t], "live": live.get(t)} for t in restored}
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)[:300]
    finally:
        run(["mysql", "-e", "DROP DATABASE IF EXISTS `%s`" % tmp])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--verify", nargs="*", default=["/etc/nginx", "/opt"])
    ap.add_argument("--dump-dir", default="/var/backups/db")
    ap.add_argument("--backup-first", action="store_true")
    a = ap.parse_args()
    e = env(a.name)
    out = {"started": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "ok": False, "steps": []}

    if a.backup_first:
        t = time.time()
        r = run(["/usr/local/sbin/server-backup-%s" % a.name], env=e)
        out["backup"] = {"ok": r.returncode == 0, "seconds": round(time.time() - t, 1),
                         "output": (r.stdout + r.stderr).strip().splitlines()[-3:]}
        if r.returncode != 0:
            print(json.dumps(out))
            return

    r = run(["restic", "snapshots", "--json", "--tag", a.name], env=e)
    snaps = sorted(json.loads(r.stdout or "[]"), key=lambda s: s["time"]) if r.returncode == 0 else []
    if not snaps:
        out["error"] = "no snapshot found: " + r.stderr.strip()[:300]
        print(json.dumps(out))
        return
    snap = snaps[-1]
    out["snapshot"] = {"id": snap.get("short_id", snap["id"][:8]), "time": snap["time"][:19].replace("T", " "),
                       "host": snap.get("hostname"), "paths": snap.get("paths", [])}
    r = run(["restic", "stats", "--json", "latest"], env=e)
    if r.returncode == 0:
        st = json.loads(r.stdout)
        out["snapshot"]["files"] = st.get("total_file_count")
        out["snapshot"]["size_mb"] = round(st.get("total_size", 0) / 1048576, 1)
    out["repository_snapshots"] = len(snaps)

    target = "/var/tmp/restore-test-%s" % a.name
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, mode=0o700)
    t = time.time()
    r = run(["restic", "restore", snap["id"], "--target", target], env=e)
    out["restore"] = {"ok": r.returncode == 0, "seconds": round(time.time() - t, 1),
                      "error": r.stderr.strip()[-300:] if r.returncode else ""}

    files = {"compared": 0, "different": [], "missing": []}
    if r.returncode == 0:
        for p in a.verify:
            compare_tree(p, target + p, files)
    files["ok"] = r.returncode == 0 and files["compared"] > 0 and not files["missing"] and not files["different"]
    out["files"] = files

    dumps = []
    restored_dump_dir = target + a.dump_dir
    if r.returncode == 0 and os.path.isdir(restored_dump_dir) and shutil.which("mysql"):
        for name in sorted(os.listdir(restored_dump_dir)):
            if name.endswith(".sql.gz"):
                dumps.append(check_dump(os.path.join(restored_dump_dir, name)))
    out["databases"] = dumps

    shutil.rmtree(target, ignore_errors=True)
    out["cleaned_up"] = not os.path.exists(target)
    out["ok"] = out["restore"]["ok"] and files["ok"] and all(d["ok"] for d in dumps)
    out["finished"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
