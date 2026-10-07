import json
import fcntl
import threading
import os
from pathlib import Path


def config_path():
    return Path(os.environ.get('LLM_USAGE_CONFIG', Path.home()/'.config/llm-usage/config.json'))


# Keys the web UI may edit. They are stored in local.json next to the database
# because systemd sandboxes mount ~/.config read-only (ProtectHome=read-only).
LOCAL_KEYS = {'subscription_prices', 'thresholds', 'refresh_seconds', 'value_alert_usd', 'notify', 'project_budgets'}


def local_path(config):
    return Path(config['database']).parent / 'local.json'


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def with_local(data):
    """Overlay the web-edited keys stored in local.json on a config mapping."""
    local = _read_json(local_path(data)) if data.get('database') else {}
    return {**data, **{key: local[key] for key in LOCAL_KEYS if key in local}}


def settings():
    return with_local(json.loads(config_path().read_text()))


_local_lock = threading.RLock()


def update_local(patch, config):
    """Commit a mapping or a current-config -> patch callable before publishing it.

    The stable sidecar inode serializes processes across atomic local.json replaces.
    Reading for a write is strict: a broken/unreadable file must not be overwritten.
    """
    path = local_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _local_lock:
        fd = os.open(path.with_name('local.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(path.read_text())
                except FileNotFoundError:
                    data = {}
                if not isinstance(data, dict):
                    raise ValueError('invalid local settings')
                current = {**config, **{k:v for k,v in data.items() if k in LOCAL_KEYS}}
                changes = patch(current) if callable(patch) else patch
                data.update(changes)
                atomic_json(path, data)
                config.update({k:v for k,v in data.items() if k in LOCAL_KEYS})
                return data
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_json(path, data):
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
