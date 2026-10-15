import argparse
import logging
import signal
import threading

from worker.main import build_worker


def main() -> None:
    ap = argparse.ArgumentParser(description="Syllabus-to-Video job worker")
    ap.add_argument("--drain", action="store_true", help="exit once the queue is empty")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = build_worker()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    worker.run_forever(stop, exit_when_idle=a.drain)


if __name__ == "__main__":
    main()
