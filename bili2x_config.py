"""Portable settings; private overrides are excluded from version control."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH_FIELDS = ("inbox_dir", "output_dir", "subtitles_dir", "history_file", "dsh_dir")


def load_config(config_path=None):
    config_path = Path(config_path) if config_path else ROOT / "config.json"
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    local_path = config_path.with_name("config.local.json")
    if local_path.exists():
        config.update(json.loads(local_path.read_text(encoding="utf-8-sig")))
    for field in PATH_FIELDS:
        if config.get(field):
            path = Path(os.path.expandvars(config[field])).expanduser()
            if not path.is_absolute():
                path = config_path.parent / path
            config[field] = str(path.resolve())
    return config


def get_api_key(config):
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    key = config.get("deepseek_api_key", "").strip()
    if key and not key.startswith("填入"):
        return key
    # Keep DSH integration available without shipping any credentials.
    creds_file = Path(config.get("dsh_dir", "~/.dsh")).expanduser() / ".credentials.yaml"
    if creds_file.exists():
        import yaml
        try:
            data = yaml.safe_load(creds_file.read_text(encoding="utf-8")) or {}
            key = data.get("refs", {}).get("DEEPSEEK_API_KEY")
            if isinstance(key, str) and key.strip():
                return key.strip()
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            pass
    raise ValueError("请设置 DEEPSEEK_API_KEY 或在忽略的 config.local.json 中配置密钥。")
