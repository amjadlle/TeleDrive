from pathlib import Path
import os

APP_NAME = "Telegram Auto Upload"

def app_data_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path

def config_path() -> Path:
    path = app_data_dir() / "config.yaml"
    if not path.exists():
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return path

def state_path() -> Path:
    path = app_data_dir() / "data" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def log_path() -> Path:
    path = app_data_dir() / "logs" / "uploader.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

DEFAULT_CONFIG = """telegram:
  api_id: 0
  api_hash: ""
  phone: ""
  target: "me"
  session_path: "data/telethon_user"
upload:
  source_dir: ""
  allowed_extensions: []
  max_file_size_mb: 0
  sleep_min_seconds: 60
  sleep_max_seconds: 90
  max_files_per_run: 3
  max_files_per_day: 100
  retry_attempts: 5
  backoff_base_seconds: 10
  floodwait_buffer_seconds: 5
  caption_template: "{name}"
  send_mode: "document"
state:
  db_path: "data/state.db"
logging:
  log_path: "logs/uploader.log"
  level: "INFO"
"""
