import threading

from .config import DATA, MUX, PORT, STATE
from .devices import Health, Muxers
from .jobs import Jobs
from .log import log
from .server import App, Server, handler
from .watcher import Watcher


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    muxers = Muxers() if MUX == "builtin" else None
    health = Health()
    jobs = Jobs(on_done=health.scan_async)
    watcher = Watcher(jobs, health)
    health.on_arrival = watcher.poke

    log(f"sideloop ({MUX} mux) on :{PORT}, data in {DATA}")
    if muxers:
        muxers.start()
    threading.Thread(target=health.loop, daemon=True).start()
    threading.Thread(target=watcher.loop, daemon=True).start()
    Server(("0.0.0.0", PORT), handler(App(jobs, health, muxers))).serve_forever()


if __name__ == "__main__":
    main()
