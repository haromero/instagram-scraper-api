"""Arranque combinado para planes con un solo proceso (por ejemplo Render free).

Levanta el worker de Redis Streams como proceso hijo y, a continuación,
Gunicorn. Si cualquiera de los dos muere, se detiene el otro para no dejar
consumidores zombie. Con ``IG_JOB_BACKEND=thread`` solo arranca Gunicorn.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def _gunicorn_command(port: str) -> list[str]:
    concurrency = os.getenv("WEB_CONCURRENCY", "1")
    return [
        sys.executable,
        "-m",
        "gunicorn",
        "app.main:app",
        "--worker-class",
        "uvicorn.workers.UvicornWorker",
        "--workers",
        concurrency,
        "--bind",
        f"0.0.0.0:{port}",
        "--timeout",
        "120",
        "--graceful-timeout",
        "30",
    ]


def main() -> None:
    processes: list[subprocess.Popen] = []
    env = os.environ.copy()
    port = env.get("PORT", "8000")

    if env.get("IG_JOB_BACKEND", "thread") == "redis":
        processes.append(
            subprocess.Popen([sys.executable, "-m", "app.worker"], env=env)
        )
    processes.append(subprocess.Popen(_gunicorn_command(port), env=env))

    def _shutdown(_signum, _frame) -> None:
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        for process in processes:
            process.wait()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            process.wait(timeout=30)
        sys.exit(max((process.returncode or 0) for process in processes))


if __name__ == "__main__":
    main()
