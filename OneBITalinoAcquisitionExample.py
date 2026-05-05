import platform
import sys
import threading
from collections import deque

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

osDic = {
    "Darwin": f"MacOS/Intel{''.join(platform.python_version().split('.')[:2])}",
    "Linux": "Linux64",
    "Windows": f"Win{platform.architecture()[0][:2]}_{''.join(platform.python_version().split('.')[:2])}",
}
if platform.mac_ver()[0] != "":
    import subprocess
    from os import linesep

    p = subprocess.Popen("sw_vers", stdout=subprocess.PIPE)
    result = p.communicate()[0].decode("utf-8").split(str("\t"))[2].split(linesep)[0]
    if result.startswith("12."):
        print("macOS version is Monterrey!")
        osDic["Darwin"] = "MacOS/Intel310"
        if (
            int(platform.python_version().split(".")[0]) <= 3
            and int(platform.python_version().split(".")[1]) < 10
        ):
            print(f"Python version required is ≥ 3.10. Installed is {platform.python_version()}")
            exit()


sys.path.append(f"PLUX-API-Python3/{osDic[platform.system()]}")

import plux


class NewDevice(plux.SignalsDev):
    def __init__(self, address):
        plux.SignalsDev.__init__(address)
        self.frequency = 0
        self.buffers = []
        self.lock = threading.Lock()
        self.stop_flag = False

    def onRawFrame(self, nSeq, data):  # onRawFrame takes three arguments
        with self.lock:
            for buf, value in zip(self.buffers, data):
                buf.append(value)
        return self.stop_flag


# example routines


def exampleAcquisition(
    address="BTH98:D3:51:FE:87:0E",
    frequency=1000,
    active_ports=[1, 2, 3, 4, 5, 6],
    window_seconds=5,
):
    """
    Example acquisition. Runs indefinitely and plots the 6 ports until window is closed.
    """
    device = NewDevice(address)
    device.frequency = int(frequency)
    window_size = device.frequency * int(window_seconds)
    device.buffers = [deque([0] * window_size, maxlen=window_size) for _ in active_ports]

    device.start(device.frequency, active_ports, 16)

    acquisition_thread = threading.Thread(target=device.loop, daemon=True)
    acquisition_thread.start()

    fig, axes = plt.subplots(len(active_ports), 1, sharex=True, figsize=(10, 8))
    if len(active_ports) == 1:
        axes = [axes]
    fig.suptitle(f"BITalino — {device.frequency} Hz")

    x = list(range(window_size))
    lines = []
    for ax, port in zip(axes, active_ports):
        (line,) = ax.plot(x, [0] * window_size)
        ax.set_ylabel(f"Port {port}")
        ax.set_ylim(0, 1024)
        ax.grid(True)
        lines.append(line)
    axes[-1].set_xlabel(f"Échantillons (fenêtre = {window_seconds} s)")

    def update(_frame):
        with device.lock:
            snapshots = [list(buf) for buf in device.buffers]
        for line, data in zip(lines, snapshots):
            line.set_ydata(data)
        return lines

    animation = FuncAnimation(fig, update, interval=50, blit=True, cache_frame_data=False)

    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        device.stop_flag = True
        acquisition_thread.join(timeout=2)
        device.stop()
        device.close()
        del animation


if __name__ == "__main__":
    # Use arguments from the terminal (if any) as the first arguments and use the remaining default values.
    exampleAcquisition(*sys.argv[1:])