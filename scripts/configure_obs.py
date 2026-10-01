"""Enable authenticated OBS control and copy its password locally, without printing it."""
import argparse
import json
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-root', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    running = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq obs64.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, check=True)
    if 'obs64.exe' in running.stdout.lower():
        parser.exit(1, 'Close OBS before configuring it; no running settings will be overwritten.\n')
    config_path = args.config_root / 'plugin_config' / 'obs-websocket' / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    password = config.get('server_password') or secrets.token_urlsafe(32)
    backup = root / 'data' / 'setup-backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        shutil.copy2(config_path, backup / 'obs-websocket.json')
    env_path = root / '.env'
    env_text = env_path.read_text(encoding='utf-8-sig') if env_path.exists() else ''
    if env_path.exists():
        shutil.copy2(env_path, backup / 'env-backup')
    config.update(server_enabled=True, auth_required=True, server_port=4455,
                  server_password=password, first_load=False)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    line = 'OBS_WS_PASSWORD=' + password
    if re.search(r'^OBS_WS_PASSWORD\s*=', env_text, re.MULTILINE):
        env_text = re.sub(r'^OBS_WS_PASSWORD\s*=.*$', lambda _: line, env_text, flags=re.MULTILINE)
    else:
        env_text = env_text.rstrip('\r\n') + '\n' + line + '\n'
    env_path.write_text(env_text, encoding='utf-8')
    print('OBS WebSocket enabled on port 4455 with authentication; password saved in ignored .env.')
    print('Previous settings saved under ignored data/setup-backups/.')


if __name__ == '__main__':
    main()
