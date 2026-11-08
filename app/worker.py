"""Public Worker startup entrypoint."""
import asyncio
from app.execution_plane.schedulers.worker_daemon import main

if __name__ == "__main__":
    asyncio.run(main())
