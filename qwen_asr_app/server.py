"""Workbench host with a one-second maintenance tick and immediate signal exit.

HTTP and WebSocket I/O still wake the event loop immediately. Only Uvicorn's
housekeeping loop is less frequent; request handling does not poll this loop.
"""
import asyncio
import argparse

import uvicorn


class IdleServer(uvicorn.Server):
    async def main_loop(self):
        self._exit_event = asyncio.Event()
        counter = 0
        while not await self.on_tick(counter):
            try:
                await asyncio.wait_for(self._exit_event.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            counter = (counter + 10) % 864000

    def handle_exit(self, sig, frame):
        super().handle_exit(sig, frame)
        event = getattr(self, '_exit_event', None)
        if event is not None:
            event.set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app', nargs='?', default='app:app')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--timeout-graceful-shutdown', type=int, default=10)
    args = parser.parse_args()
    IdleServer(uvicorn.Config(args.app, host=args.host, port=args.port,
                             timeout_graceful_shutdown=args.timeout_graceful_shutdown)).run()


if __name__ == '__main__':
    main()
