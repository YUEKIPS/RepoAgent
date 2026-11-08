"""Task-scoped connection pool; no request retry or model conversation state."""

import asyncio


class RequestTransport:
    def __init__(self, factory, timeout):
        self.runner = asyncio.Runner()
        self.client = factory(timeout)
        self.entered = self.closed = False

    def run(self, coroutine):
        if self.closed:
            coroutine.close()
            raise RuntimeError("Transport closed")
        if not self.entered:
            self.runner.run(self.client.__aenter__())
            self.entered = True
        return self.runner.run(coroutine)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.entered:
                self.runner.run(self.client.__aexit__(None, None, None))
        finally:
            self.runner.close()
