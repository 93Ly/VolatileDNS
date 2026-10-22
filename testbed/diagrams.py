"""Draw the two as-built diagrams used in the report (results/fig_testbed.png, fig_simulation.png)."""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

plt.rcParams.update({"font.family": "serif", "font.size": 9})


def box(ax, x, y, w, h, text, fc="white", ec="black", lw=1.2, fs=9, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                fc=fc, ec=ec, lw=lw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            fontweight="bold" if bold else "normal", wrap=True)


def arrow(ax, x0, y0, x1, y1, text="", both=False, dy=0.12, ls="-"):
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                arrowprops=dict(arrowstyle="<->" if both else "->", lw=1.2, ls=ls))
    if text:
        ax.text((x0 + x1) / 2, (y0 + y1) / 2 + dy, text, ha="center", va="bottom", fontsize=8)


def testbed(path):
    fig, ax = plt.subplots(figsize=(9, 4.6))
    ax.set_xlim(0, 12); ax.set_ylim(0, 5.4); ax.axis("off")
    box(ax, 0.2, 3.4, 2.1, 1.2, "Test client\n(dnspython)", fc="#eeeeee")
    box(ax, 4.0, 3.2, 3.0, 1.6, "VolatileDNS proxy\nUDP + TCP :5300\nlistener, cache,\npolicy, history", bold=True)
    box(ax, 9.0, 3.4, 2.7, 1.2, "Test DNS server\n127.0.0.1:5301\nzone college.test", fc="#eeeeee")
    box(ax, 4.0, 0.9, 3.0, 1.0, "SQLite\ncache | history | decisions")
    box(ax, 9.0, 0.9, 2.7, 1.0, "Fault-injection\ncontroller (port 5302)", fc="#eeeeee")
    box(ax, 0.2, 0.9, 2.7, 1.0, "Web dashboard", fc="white")
    arrow(ax, 2.3, 4.0, 4.0, 4.0, "query / answer", both=True)
    arrow(ax, 7.0, 4.0, 9.0, 4.0, "query / answer", both=True)
    arrow(ax, 5.5, 3.2, 5.5, 1.9, "log", dy=0.05)
    arrow(ax, 10.35, 1.9, 10.35, 3.4, "down / up /\nmove / delete", dy=0.0)
    arrow(ax, 2.9, 1.4, 4.0, 1.4, "reads")
    ax.plot([1.55, 1.55, 9.0], [0.9, 0.35, 0.35], "k--", lw=1.2)
    ax.annotate("", xy=(9.6, 0.9), xytext=(9.6, 0.35), arrowprops=dict(arrowstyle="->", lw=1.2, ls="--"))
    ax.plot([9.0, 9.6], [0.35, 0.35], "k--", lw=1.2)
    ax.text(5.3, 0.42, "dashboard buttons call the controller", ha="center", va="bottom", fontsize=8, style="italic")
    ax.text(2.3, 5.0, "downstream", ha="center", fontsize=9, color="#555555")
    ax.text(10.35, 5.0, "upstream", ha="center", fontsize=9, color="#555555")
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def simulation(path):
    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax.set_xlim(0, 12); ax.set_ylim(0, 6); ax.axis("off")
    box(ax, 0.2, 3.9, 3.0, 1.4, "Client poller\nasks every name every 5 s\n(virtual time)", fc="#eeeeee")
    box(ax, 4.3, 3.7, 3.3, 1.8, "VolatileDNS engine\n(the real code:\ncache, history, policy)", bold=True)
    box(ax, 8.7, 3.9, 3.1, 1.4, "Simulated DNS server\nanswers from the world,\ndown during the outage", fc="#eeeeee")
    box(ax, 8.7, 1.2, 3.1, 1.4, "World (ground truth)\nlive IPs per name,\nrandom moves, CDN pool", fc="#eeeeee")
    box(ax, 4.3, 1.2, 3.3, 1.4, "Scorer\nanswer address live?\nok | wrong | SERVFAIL")
    box(ax, 0.2, 1.2, 3.0, 1.4, "Virtual clock\nsets the time of every call", fc="#eeeeee")
    arrow(ax, 3.2, 4.6, 4.3, 4.6, "query")
    arrow(ax, 7.6, 4.6, 8.7, 4.6, "ask / answer / fail", both=True)
    arrow(ax, 10.25, 3.9, 10.25, 2.6, "reads")
    arrow(ax, 5.95, 3.7, 5.95, 2.6, "answer", dy=0.05)
    arrow(ax, 8.7, 1.9, 7.6, 1.9, "which IPs work?")
    arrow(ax, 1.7, 2.6, 4.3, 3.9, "", ls="--")
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs("results", exist_ok=True)
    testbed("results/fig_testbed.png")
    simulation("results/fig_simulation.png")
    print("diagrams written")
