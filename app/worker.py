"""Compatibility entry point for the execution-plane worker daemon."""
import asyncio
from app.execution_plane.schedulers.worker_daemon import *
if __name__ == "__main__":
    asyncio.run(main())
