"""Start the existing IPP server under pythonw, with no console or queue setup."""
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent.parent


def bridge_running(port=8631):
    # Always connect directly to loopback, even with a configured HTTP proxy.
    try:
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/ipp/print", timeout=3) as response:
            return response.status == 200 and response.read(512).startswith(
                b"HP LaserJet P1006 local IPP bridge.")
    except OSError:
        return False


def run(port=8631):
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    (ROOT / "state").mkdir(exist_ok=True)
    # pythonw supplies None for the standard streams. Redirect before imports
    # so both normal diagnostics and unexpected tracebacks are retained.
    with (ROOT / "state/background.log").open("a", encoding="utf-8", buffering=1) as log, \
            open(os.devnull, "r", encoding="utf-8") as input_stream:
        original_streams = sys.stdout, sys.stderr, sys.stdin
        sys.stdout = sys.stderr = log
        sys.stdin = input_stream
        print(f"\n{datetime.now(timezone.utc).isoformat()} Background launcher PID={os.getpid()}")
        try:
            if bridge_running(port):
                print("Bridge already responds; keeping the existing process and its jobs.")
                return 0
            from p1006.__main__ import main
            # No dry run, queue installation, firmware upload or test page on
            # startup. The bridge opens USB only when an actual job arrives.
            sys.argv = [str(Path(__file__)), "serve", "--port", str(port)]
            main()
            return 0
        except KeyboardInterrupt:
            print("Background bridge interrupted.")
            return 130
        except Exception:
            traceback.print_exc()
            return 1
        finally:
            sys.stdout, sys.stderr, sys.stdin = original_streams


if __name__ == "__main__":
    raise SystemExit(run())
