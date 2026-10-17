"""Print the selected program whenever it changes, and log Perfect Cue presses.

    python examples/minimal.py /dev/tty.usbserial-A1 [/dev/tty.usbserial-B2]
    python examples/minimal.py sim sim
"""
import sys
import time

from dsan import Limitimer, LinkState, PerfectCue


def open_limitimer(port):
    return Limitimer.simulated() if port == "sim" else Limitimer(port)


def open_perfectcue(port):
    return PerfectCue.simulated() if port == "sim" else PerfectCue(port)


lt = open_limitimer(sys.argv[1] if len(sys.argv) > 1 else "sim")


@lt.on_change
def show(state, previous):
    p = state.selected
    flag = "OVER" if p.overtime else "WARN" if p.warning else ""
    print(f"P{p.number} {state.display(p.number):>9} {flag}")


@lt.on_link
def link(status):
    if status == LinkState.STALE:
        print("!! Limitimer data stopped — check the cable")


lt.start()

if len(sys.argv) > 2:
    cue = open_perfectcue(sys.argv[2])
    cue.on_button(lambda b: print(f"Perfect Cue: {b.name}"))
    cue.start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
