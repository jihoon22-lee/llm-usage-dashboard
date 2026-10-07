import json
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


def update_local(patch, config):
    path = local_path(config)
    data = _read_json(path)
    data.update(patch)
    atomic_json(path, data)
    return data


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
