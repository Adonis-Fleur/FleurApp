import os
import socket
import threading
import time

import httpx

BACKEND_PORT = int(os.environ.get('MAILACCESS_BACKEND_PORT', '8731'))
BACKEND_URL = f'http://127.0.0.1:{BACKEND_PORT}'

_lock = threading.Lock()
_client = None


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) != 0


def ensure_backend():
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        import uvicorn
        from backend.main import app as mailaccess_app

        config = uvicorn.Config(
            mailaccess_app,
            host='127.0.0.1',
            port=BACKEND_PORT,
            log_level='warning',
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        for _ in range(100):
            if not _port_free(BACKEND_PORT):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                f'MailAccess backend failed to start on port {BACKEND_PORT}'
            )

        _client = httpx.Client(base_url=BACKEND_URL, timeout=180)
        return _client