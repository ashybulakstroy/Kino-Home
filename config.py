import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
ENV_FILE = BASE_DIR / '.env'


def _read_env():
    cfg = {}
    env_path = ENV_FILE
    if env_path.exists():
        for line in env_path.read_text('utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            cfg[key.strip()] = val.strip()
    return cfg


_env = _read_env()


TEMP_DIR = BASE_DIR / _env.get('TEMP_DIR', 'data/temp')
MAX_TEMP_SIZE_GB = int(_env.get('MAX_TEMP_SIZE_GB', '10'))
TEMP_MAX_AGE_DAYS = int(_env.get('TEMP_MAX_AGE_DAYS', '7'))
TOPIC_MAX_AGE_DAYS = int(_env.get('TOPIC_MAX_AGE_DAYS', '90'))
LISTING_CACHE_MAX_AGE_DAYS = int(_env.get('LISTING_CACHE_MAX_AGE_DAYS', '1'))
MAX_TEMP_SIZE_BYTES = MAX_TEMP_SIZE_GB * (1024 ** 3)
TEMP_MAX_AGE_SECS = TEMP_MAX_AGE_DAYS * 86400
MAX_TEMP_FILES = int(_env.get('MAX_TEMP_FILES', '5'))
READAHEAD_SECONDS = int(_env.get('READAHEAD_SECONDS', '900'))
READAHEAD_RECHARGE_SECONDS = int(_env.get('READAHEAD_RECHARGE_SECONDS', '300'))
READAHEAD_MAX_BYTES = int(_env.get('READAHEAD_MAX_MB', '128')) * (1024 ** 2)
SERVER_PORT = int(_env.get('SERVER_PORT', '8765'))
ENRICH_INTERVAL_MINUTES = int(_env.get('ENRICH_INTERVAL_MINUTES', '15'))
PUBLIC_MODE = _env.get('PUBLIC_MODE', '0').strip().lower() in ('1', 'true', 'yes', 'on')
