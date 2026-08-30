import os
import json
import importlib

ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(ROOT, 'apps_config.json')
APPS_DIR = os.path.join(ROOT, 'apps')


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        return {'enabled_apps': ['home'], 'port': 5326}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_enabled_apps():
    return _load_config().get('enabled_apps', [])


def get_all_apps():
    if not os.path.isdir(APPS_DIR):
        return []
    return sorted([
        d for d in os.listdir(APPS_DIR)
        if os.path.isdir(os.path.join(APPS_DIR, d))
    ])


def get_app_info(app_name):
    info_path = os.path.join(APPS_DIR, app_name, 'app.json')
    name = app_name.replace('_', ' ').title()
    description = ''
    route = '/' + app_name
    if os.path.exists(info_path):
        with open(info_path) as f:
            meta = json.load(f)
            name = meta.get('name', name)
            description = meta.get('description', '')
            route = meta.get('route', route)
    return {'name': name, 'description': description, 'route': route}


def get_apps_info():
    enabled = set(get_enabled_apps())
    all_apps = get_all_apps()
    result = []
    for app_name in all_apps:
        info = get_app_info(app_name)
        info['key'] = app_name
        info['enabled'] = app_name in enabled
        result.append(info)
    result.sort(key=lambda x: (not x['enabled'], x['name']))
    return result


def discover_blueprints():
    enabled = get_enabled_apps()
    blueprints = []
    for app_name in enabled:
        try:
            path = os.path.join(APPS_DIR, app_name)
            if not os.path.isdir(path):
                print(f"Warning: App folder '{app_name}' not found, skipping")
                continue
            module = importlib.import_module(f'apps.{app_name}')
            if hasattr(module, 'bp'):
                blueprints.append(module.bp)
        except ImportError as e:
            print(f"Warning: Could not load app '{app_name}': {e}")
    return blueprints
