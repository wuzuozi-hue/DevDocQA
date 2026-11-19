"""Native launcher, including psycopg's Windows selector-loop requirement."""
import asyncio
import os
import sys

import uvicorn


if __name__ == "__main__":
    config = uvicorn.Config("app.main:app", host=os.getenv("WORKER_HOST", "127.0.0.1"),
                            port=int(os.getenv("WORKER_PORT", "8001")), loop="asyncio", access_log=False)
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        # Recent Uvicorn selects Proactor internally in Server.run(); psycopg
        # requires Selector. Own the loop explicitly, independent of that default.
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.close()
    else:
        asyncio.run(server.serve())
