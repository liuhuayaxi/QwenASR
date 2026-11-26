"""Workbench host with a one-second maintenance tick and immediate signal exit.

HTTP and WebSocket I/O still wake the event loop immediately. Only Uvicorn's
housekeeping loop is less frequent; request handling does not poll this loop.
"""
import argparse
import os


def __getattr__(name):
    if name=='IdleServer':
        from .http_runtime import IdleServer
        return IdleServer
    raise AttributeError(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app', nargs='?', default='app:app')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--timeout-graceful-shutdown', type=int, default=10)
    args = parser.parse_args()
    if os.getenv('QWEN_ASR_STANDBY_STATE'):
        from .standby import run
    else:
        from .http_runtime import run
    run(args)


if __name__ == '__main__':
    main()
