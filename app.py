"""Proxmox TUI Panel — full-screen dashboard for containers and VMs on a PVE host.

Runs on the host itself (no SSH): reads state from `pvesh get /cluster/resources`
and acts through `pct` / `qm`. Meant to be wrapped in kmscon on tty1 so it shows
up on the machine's own monitor, but it works in any terminal.

Layout: a mosaic of cards. Columns come from the available width; rows always
keep a card's full height and the mosaic scrolls when they do not all fit.
Running machines are grouped first, stopped ones after.

Each card:
  ● [CT 101] web-frontend                    up 2d 4h
  ⚙ ████░░░░░░░░   4.0%  · 4 vCPU
  ▤ ███░░░░░░░░░  29.0%  · 1.2 / 4.0 GB
  ▦ ████░░░░░░░░  37.0%  · 15 / 40 GB
  ◈ ░░░░░░░░░░░░     --
  ⇅ ↓ 115K/s   ↑ 38K/s   · Σ ↓524M ↑91M
  [ PARAR ] [ CONSOLA ]

Usage: app.py [--filtro=todas|vivas|apagadas]
"""
from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Header, Static


@dataclass
class Machine:
    vmid:   int
    name:   str
    status: str       # "running" | "stopped" | ...
    kind:   str       # "ct" | "vm"
    cpu:    float     # 0.0 - 1.0 (fracción del total de vCPUs asignados)
    mem:    int       # bytes
    maxmem: int       # bytes
    disk:   int       # bytes
    maxdisk: int      # bytes
    maxcpu: int = 0   # nº de vCPUs asignados
    uptime: int = 0   # segundos en marcha (0 = apagada)
    netin:  int = 0   # bytes acumulados rx
    netout: int = 0   # bytes acumulados tx
    net_in_rate:  float = 0.0   # bytes/s rx (calculado entre muestras)
    net_out_rate: float = 0.0   # bytes/s tx
    gpu:    Optional[float] = None   # 0-100 sm% NVIDIA, None = no medible
    gpu_passthrough: Optional[str] = None  # "nvidia" | "igpu" | None — la GPU está pasada a esta VM

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def cpu_pct(self) -> float:
        return max(0.0, min(100.0, self.cpu * 100))

    @property
    def mem_pct(self) -> float:
        if not self.maxmem:
            return 0.0
        return max(0.0, min(100.0, self.mem / self.maxmem * 100))

    @property
    def disk_pct(self) -> float:
        if not self.maxdisk:
            return 0.0
        return max(0.0, min(100.0, self.disk / self.maxdisk * 100))

    @property
    def gpu_pct(self) -> float:
        return 0.0 if self.gpu is None else max(0.0, min(100.0, self.gpu))


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=10,
        stdin=subprocess.DEVNULL,
    )


# ---------- Configuración externa ----------
# En el código NO vive nada propio de una máquina concreta: los slots PCI de las
# GPUs con passthrough y el subtítulo se leen de /etc/lxc-panel/config.json, que
# no forma parte del repositorio. Sin fichero, el panel funciona igual (solo
# pierde el marcado VFIO de la fila GPU).
CONFIG_PATH = "/etc/lxc-panel/config.json"
_CONFIG_DEFAULTS: dict = {
    # {"prefijo_pci": "etiqueta"} → la fila GPU muestra VFIO·<primera letra>
    "gpu_passthrough_pci": {},
    # Texto a la derecha del título; vacío = hostname de la máquina
    "subtitulo": "",
}


def load_config() -> dict:
    cfg = dict(_CONFIG_DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update(data)
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("config ilegible: %s", CONFIG_PATH)
    return cfg


CONFIG = load_config()


_CG_LXC  = re.compile(r"/lxc(?:\.payload)?[./](\d+)")
_CG_QEMU = re.compile(r"/(\d+)\.scope")


def _pid_to_vmid(pid: int) -> Optional[int]:
    """Mapea PID → VMID via cgroup. CT: `/lxc.payload.<id>`. VM: `/<id>.scope`."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            cg = f.read()
    except Exception:
        return None
    m = _CG_LXC.search(cg)
    if m:
        return int(m.group(1))
    m = _CG_QEMU.search(cg)
    if m:
        return int(m.group(1))
    return None


def get_passthrough_map() -> dict[int, str]:
    """{vmid: 'nvidia'|'igpu'} para VMs con `hostpci*` en su config.

    Sólo VMs (qm). CTs con NVIDIA (101/106) no son passthrough — comparten
    /dev/nvidia* con el host vía cgroup devices.allow, así que su uso sí lo
    mide pmon."""
    import glob
    out: dict[int, str] = {}
    for path in glob.glob("/etc/pve/qemu-server/*.conf"):
        try:
            vmid = int(path.rsplit("/", 1)[-1].split(".")[0])
        except ValueError:
            continue
        try:
            with open(path) as f:
                content = f.read()
        except Exception:
            continue
        # Parsear solo header (antes de la primera [seccion] de snapshot)
        for raw in content.splitlines():
            line = raw.strip()
            if line.startswith("["):
                break
            if line.startswith("hostpci"):
                for pci, etiqueta in (CONFIG.get("gpu_passthrough_pci") or {}).items():
                    if pci and pci in line:
                        out[vmid] = str(etiqueta)
                        break
    return out


def get_gpu_per_vmid() -> tuple[dict[int, int], bool]:
    """Sm% NVIDIA por VMID. Devuelve (dict, gpu_available).
    Si nvidia-smi falla (típico cuando la 3080 está bound a vfio-pci), gpu_available=False."""
    try:
        r = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"],
                           capture_output=True, text=True, timeout=4)
    except Exception:
        return {}, False
    if r.returncode != 0:
        return {}, False
    per: dict[int, int] = defaultdict(int)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[1])
        except Exception:
            continue
        sm_raw = parts[3]
        try:
            sm = int(sm_raw) if sm_raw != "-" else 0
        except Exception:
            sm = 0
        vmid = _pid_to_vmid(pid)
        if vmid is not None:
            per[vmid] += sm
    return dict(per), True


# Muestra anterior de contadores de red por VMID → {vmid: (t, netin, netout)}.
# Sirve para derivar la tasa (bytes/s) entre refrescos.
_NET_PREV: dict[int, tuple[float, int, int]] = {}


def list_machines() -> list[Machine]:
    """Una sola llamada a `pvesh get /cluster/resources --type vm` + un sample
    de `nvidia-smi pmon` para el % GPU por máquina."""
    out: list[Machine] = []
    now = time.monotonic()
    r = _run(["pvesh", "get", "/cluster/resources", "--type", "vm",
              "--output-format", "json"])
    if r.returncode != 0:
        logging.warning("pvesh failed rc=%d stderr=%r", r.returncode, r.stderr.strip())
        return out
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError:
        return out

    gpu_per_vmid, gpu_available = get_gpu_per_vmid()
    pt_map = get_passthrough_map()
    for m in items:
        try:
            vmid = int(m["vmid"])
        except (KeyError, ValueError, TypeError):
            continue
        kind = "ct" if m.get("type") == "lxc" else "vm"
        status = str(m.get("status") or "?").lower()

        # Passthrough activo: la VM tiene la GPU dedicada → la marcamos como
        # "VFIO" en la card, independiente de lo que diga pmon.
        gpu_pt = pt_map.get(vmid) if (kind == "vm" and status == "running") else None
        if gpu_pt is not None:
            gpu_val: Optional[float] = None   # bandera para "VFIO" (junto a gpu_passthrough)
        elif gpu_available:
            gpu_val = float(gpu_per_vmid.get(vmid, 0))
        else:
            gpu_val = None   # 3080 en VFIO o nvidia-smi no disponible

        netin  = int(m.get("netin") or 0)
        netout = int(m.get("netout") or 0)
        rin = rout = 0.0
        prev = _NET_PREV.get(vmid)
        if prev is not None and status == "running":
            dt = now - prev[0]
            if dt > 0:
                rin  = max(0.0, (netin  - prev[1]) / dt)
                rout = max(0.0, (netout - prev[2]) / dt)
        _NET_PREV[vmid] = (now, netin, netout)

        out.append(Machine(
            vmid    = vmid,
            name    = str(m.get("name") or "?"),
            status  = status,
            kind    = kind,
            cpu     = float(m.get("cpu") or 0.0),
            mem     = int(m.get("mem") or 0),
            maxmem  = int(m.get("maxmem") or 0),
            disk    = int(m.get("disk") or 0),
            maxdisk = int(m.get("maxdisk") or 0),
            maxcpu  = int(m.get("maxcpu") or 0),
            uptime  = int(m.get("uptime") or 0),
            netin   = netin,
            netout  = netout,
            net_in_rate  = rin,
            net_out_rate = rout,
            gpu     = gpu_val,
            gpu_passthrough = gpu_pt,
        ))
    out.sort(key=lambda x: x.vmid)
    return out


def machine_action(kind: str, action: str, vmid: int) -> tuple[bool, str]:
    import time
    tool = "pct" if kind == "ct" else "qm"
    unit = f"lxc-panel-{kind}-{action}-{vmid}-{int(time.time())}"
    if action == "start":
        cmd = ["systemd-run", "--no-block", f"--unit={unit}", tool, "start", str(vmid)]
    elif action == "shutdown":
        cmd = ["systemd-run", "--no-block", f"--unit={unit}",
               tool, "shutdown", str(vmid), "--timeout", "120", "--forceStop", "1"]
    else:
        return False, f"unknown action {action}"
    logging.info("machine_action cmd=%s", cmd)
    r = _run(cmd)
    logging.info("machine_action rc=%d stdout=%r stderr=%r",
                 r.returncode, r.stdout.strip(), r.stderr.strip())
    return r.returncode == 0, r.stderr.strip() or r.stdout.strip()


# ---------- Barras de progreso ----------
# Bloque lleno coloreado sobre un carril gris (sin '░', que en la fuente del
# monitor renderiza como un dithering sucio tipo "nieve de TV").
_BLOCK   = "█"
_BAR_LEN = 12
_TRACK   = "#333333"         # color del carril de fondo (bloque "vacío")

# Iconos de las filas. Son símbolos MONOCROMOS a propósito: kmscon usa DejaVu
# Sans Mono, que los tiene todos, pero NO tiene emoji de color (🖥 💤 🟢 …) —
# esos saldrían como cajas vacías en el monitor del host. Verificado con
# `fc-query -f "%{charset}"` sobre DejaVuSansMono.ttf.
_IC_CPU  = "⚙"
_IC_MEM  = "▤"
_IC_DISK = "▦"
_IC_GPU  = "◈"
_IC_NET  = "⇅"

# Icono + palabra. El icono solo era ambiguo (▤ y ▦ se parecen demasiado); la
# palabra al lado cuesta 4 celdas por fila y las tarjetas ya dan de sí.
_LBL_CPU  = f"{_IC_CPU} CPU"
_LBL_MEM  = f"{_IC_MEM} MEM"
_LBL_DISK = f"{_IC_DISK} DISK"
_LBL_GPU  = f"{_IC_GPU} GPU"
_LBL_NET  = f"{_IC_NET} NET"


def _style_for(pct: float) -> str:
    if pct >= 80: return "red"
    if pct >= 50: return "yellow"
    return "green"


def _solid_bar(pct: float, color: str) -> str:
    pct = max(0.0, min(100.0, pct))
    fill = max(0, min(_BAR_LEN, int(round(pct / 100 * _BAR_LEN))))
    return f"[{color}]{_BLOCK * fill}[/][{_TRACK}]{_BLOCK * (_BAR_LEN - fill)}[/]"


def _fmt_bytes(n: float) -> str:
    """Bytes → '1.2G' / '334M' / '45K' / '0B' (compacto, sin espacio)."""
    n = float(n)
    for unit, div in (("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= div:
            v = n / div
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return f"{int(n)}B"


def _fmt_rate(bps: float) -> str:
    return f"{_fmt_bytes(bps)}/s"


def _fmt_gib_pair(used: int, total: int) -> str:
    """'5.4 / 16 GB' en GiB."""
    g = 1 << 30
    u, t = used / g, total / g
    us = f"{u:.1f}" if u < 10 else f"{u:.0f}"
    ts = f"{t:.1f}" if t < 10 else f"{t:.0f}"
    return f"{us} / {ts} GB"


def _fmt_uptime(secs: int) -> str:
    if secs <= 0:
        return ""
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:  return f"{d}d {h}h"
    if h:  return f"{h}h {m}m"
    if m:  return f"{m}m"
    return f"{secs}s"


def _metric_line(label: str, pct: float, value: Optional[float] = None,
                 extra: str = "") -> str:
    """`value=None` se renderiza como '--' (p.ej. nvidia-smi no medible).
    `extra` añade texto atenuado tras el % (vCPUs, GB absolutos, …)."""
    if value is None:
        base = f"{label:<6} [{_TRACK}]{_BLOCK * _BAR_LEN}[/]     --"
    else:
        base = f"{label:<6} {_solid_bar(pct, _style_for(pct))} {pct:5.1f}%"
    if extra:
        base += f"   [dim]{extra}[/]"
    return base


def _net_line(m: "Machine") -> str:
    """Fila de red: tasa ↓/↑ + total acumulado."""
    if not m.is_running:
        return f"{_LBL_NET} [dim]—[/]"
    return (f"{_LBL_NET} [green]↓[/] {_fmt_rate(m.net_in_rate):>9}   "
            f"[cyan]↑[/] {_fmt_rate(m.net_out_rate):>9}"
            f"   [dim]· Σ ↓{_fmt_bytes(m.netin)} ↑{_fmt_bytes(m.netout)}[/]")


def _gpu_line(m: "Machine") -> str:
    """Línea GPU específica: distingue VFIO (NVIDIA/iGPU) vs. medición pmon."""
    if m.gpu_passthrough:
        marca = str(m.gpu_passthrough)[:1]
        return f"{_LBL_GPU} [cyan]{_BLOCK * _BAR_LEN}[/] VFIO·{marca}"
    return _metric_line(_LBL_GPU, m.gpu_pct, m.gpu)


# ---------- UI ----------
class ActionChip(Static):
    """Botón-chip de 1 línea. El `Button` de Textual no pinta su etiqueta a
    `height: 1`, así que usamos un Static enfocable y clicable que sí la muestra."""

    can_focus = True

    DEFAULT_CSS = """
    ActionChip {
        height: 1; width: 1fr; min-width: 9; margin-right: 1;
        text-align: center; content-align: center middle;
        text-style: bold; color: white;
    }
    ActionChip:focus  { text-style: bold reverse; }
    ActionChip.hidden { display: none; }
    ActionChip.act-start   { background: $success; color: black; }
    ActionChip.act-stop    { background: $error;   color: white; }
    ActionChip.act-console { background: $primary; color: white; }
    """

    class Pressed(Message):
        def __init__(self, action: str, vmid: int) -> None:
            self.action = action
            self.vmid = vmid
            super().__init__()

    def __init__(self, label: str, action: str, vmid: int,
                 variant: str, hidden: bool = False) -> None:
        cls = f"act-{variant}" + (" hidden" if hidden else "")
        super().__init__(f" {label} ", id=f"{action}-{vmid}", classes=cls)
        self.action = action
        self.vmid = vmid

    def on_click(self) -> None:
        self.post_message(self.Pressed(self.action, self.vmid))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            self.post_message(self.Pressed(self.action, self.vmid))


class ScrollChip(Static):
    """Flecha pulsable para desplazar un mosaico con el dedo.

    En un terminal no existe el evento "táctil": la aplicación solo recibe teclas
    y ratón. Un deslizamiento solo llega si el emulador lo traduce a rueda, y el
    xterm.js de la Shell web de Proxmox no lo hace mientras la app tiene activado
    el seguimiento de ratón. Un TOQUE, en cambio, sí llega como clic — así que la
    forma de poder desplazar con el dedo es ofrecer algo que tocar.
    """

    can_focus = True

    DEFAULT_CSS = """
    ScrollChip {
        height: 1; width: 1fr; text-align: center; content-align: center middle;
        text-style: bold; color: $text; background: #334155;
        margin-right: 1;
    }
    ScrollChip:hover { background: $primary; color: white; }
    ScrollChip:focus { text-style: bold reverse; }
    """

    class Pressed(Message):
        def __init__(self, grid_id: str, hacia: int) -> None:
            self.grid_id = grid_id
            self.hacia = hacia          # -1 arriba, +1 abajo
            super().__init__()

    def __init__(self, grid_id: str, hacia: int) -> None:
        super().__init__("▲  subir" if hacia < 0 else "▼  bajar")
        self.grid_id = grid_id
        self.hacia = hacia

    def on_click(self) -> None:
        self.post_message(self.Pressed(self.grid_id, self.hacia))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            self.post_message(self.Pressed(self.grid_id, self.hacia))


class MachineCard(Container):
    """Tarjeta por máquina dentro del Grid mosaico."""

    DEFAULT_CSS = """
    MachineCard {
        /* el alto lo fija grid-rows del padre (#mosaic) — no marcar aquí */
        padding: 0 1;
        border: heavy $panel-darken-2;
        background: $surface;
    }
    MachineCard.running { border: heavy $success; }
    MachineCard.stopped { border: heavy $panel-darken-2; }
    MachineCard.ct .title { color: $accent; }
    MachineCard.vm .title { color: $warning; }
    MachineCard .title    { width: 1fr; text-style: bold;
                        text-wrap: nowrap; text-overflow: ellipsis; }
    MachineCard .status   { width: 10; content-align: right middle; text-style: bold; }
    MachineCard.running .status { color: $success; }
    MachineCard.stopped .status { color: $text-muted; }
    MachineCard .metric   { height: 1; padding: 0 1; }
    MachineCard #buttons  { height: 1; align-horizontal: left; margin-top: 0; }
    """

    def __init__(self, m: Machine) -> None:
        super().__init__(id=f"card-{m.vmid}")
        self.vmid = m.vmid
        self._m = m
        self.add_class(m.kind)
        self.add_class("running" if m.is_running else "stopped")

    def compose(self) -> ComposeResult:
        m = self._m
        yield Horizontal(
            Static(self._title_text(m), classes="title", id=f"title-{m.vmid}"),
            Static(self._status_text(m), classes="status", id=f"status-{m.vmid}"),
        )
        yield Static(self._cpu_text(m),  classes="metric", id=f"cpu-{m.vmid}",  markup=True)
        yield Static(self._mem_text(m),  classes="metric", id=f"mem-{m.vmid}",  markup=True)
        yield Static(self._disk_text(m), classes="metric", id=f"disk-{m.vmid}", markup=True)
        yield Static(_gpu_line(m),       classes="metric", id=f"gpu-{m.vmid}",  markup=True)
        yield Static(_net_line(m),       classes="metric", id=f"net-{m.vmid}",  markup=True)
        yield Horizontal(
            ActionChip("INICIAR", "start",   m.vmid, "start",   hidden=m.is_running),
            ActionChip("PARAR",   "stop",    m.vmid, "stop",    hidden=not m.is_running),
            ActionChip("CONSOLA", "console", m.vmid, "console",
                       hidden=not (m.kind == "ct" and m.is_running)),
            id="buttons",
        )

    @staticmethod
    def _title_text(m: Machine) -> str:
        """Punto de estado coloreado + etiqueta. El '●'/'○' garantiza que el
        estado se vea aunque el borde verde no se distinga a 3 columnas."""
        dot = "[green]●[/]" if m.is_running else "[grey42]○[/]"
        return f"{dot} [{m.kind.upper()} {m.vmid}] {m.name}"

    @staticmethod
    def _status_text(m: Machine) -> str:
        """Derecha del título: uptime si está encendida, si no 'apagada'."""
        if m.is_running:
            up = _fmt_uptime(m.uptime)
            return f"up {up}" if up else "up"
        return ""

    @staticmethod
    def _cpu_text(m: Machine) -> str:
        extra = f"· {m.maxcpu} vCPU" if m.maxcpu else ""
        return _metric_line(_LBL_CPU, m.cpu_pct, m.cpu_pct, extra)

    @staticmethod
    def _mem_text(m: Machine) -> str:
        extra = f"· {_fmt_gib_pair(m.mem, m.maxmem)}" if m.maxmem else ""
        return _metric_line(_LBL_MEM, m.mem_pct, m.mem_pct, extra)

    @staticmethod
    def _disk_text(m: Machine) -> str:
        extra = f"· {_fmt_gib_pair(m.disk, m.maxdisk)}" if m.maxdisk else ""
        return _metric_line(_LBL_DISK, m.disk_pct, m.disk_pct, extra)

    @property
    def kind(self) -> str:
        return self._m.kind

    def update_machine(self, m: Machine) -> None:
        self._m = m
        self.set_class(m.is_running, "running")
        self.set_class(not m.is_running, "stopped")
        try:
            self.query_one(f"#title-{self.vmid}",  Static).update(self._title_text(m))
            self.query_one(f"#status-{self.vmid}", Static).update(self._status_text(m))
            self.query_one(f"#cpu-{self.vmid}",   Static).update(self._cpu_text(m))
            self.query_one(f"#mem-{self.vmid}",   Static).update(self._mem_text(m))
            self.query_one(f"#disk-{self.vmid}",  Static).update(self._disk_text(m))
            self.query_one(f"#gpu-{self.vmid}",   Static).update(_gpu_line(m))
            self.query_one(f"#net-{self.vmid}",   Static).update(_net_line(m))
            self.query_one(f"#start-{self.vmid}",   ActionChip).set_class(m.is_running, "hidden")
            self.query_one(f"#stop-{self.vmid}",    ActionChip).set_class(not m.is_running, "hidden")
            self.query_one(f"#console-{self.vmid}", ActionChip).set_class(
                not (m.kind == "ct" and m.is_running), "hidden")
        except Exception:
            # widget aún no compuesto o nodo eliminado — ignoramos
            pass


class LXCPanel(App):
    CSS = """
    Screen { layout: vertical; background: $background; }
    #toolbar {
        height: 1; padding: 0 2; background: $primary;
        color: $text; text-style: bold; content-align: left middle;
    }
    #mosaic, #mosaic-vivas, #mosaic-apagadas {
        layout: grid;
        grid-size: 2;             /* nº de columnas — se recalcula en _relayout */
        grid-gutter: 0 1;         /* sin separación vertical (gana filas para las tarjetas) */
        padding: 0 2;
        height: 1fr;              /* ocupa todo el hueco entre toolbar y status */
        overflow-y: auto;         /* si no caben, scroll (nunca recortar la tarjeta) */
        scrollbar-size-vertical: 1;
        /* Alto de fila FIJO = tarjeta completa. Con `1fr` las filas se repartían
           el alto disponible, la tarjeta se aplastaba y lo primero en caer era la
           fila de botones (el bug). El nº de columnas se elige en _relayout para
           llenar la pantalla; si aun así no cabe, se hace scroll. */
        grid-rows: 9;
    }
    /* Barra de flechas táctiles; solo se muestra si el mosaico desborda. */
    .scrollbar-tactil { height: 1; padding: 0 2; display: none; }
    .scrollbar-tactil.visible { display: block; }
    /* Pantalla partida: encendidas a la izquierda, apagadas a la derecha. */
    #split { height: 1fr; }
    #split .col { width: 1fr; }
    .col-title { height: 1; padding: 0 2; text-style: bold; }
    .col-title.on  { color: $success; }
    .col-title.off { color: $text-muted; }
    #status-line {
        height: 1; padding: 0 2; background: $boost;
        color: $text; text-style: bold; content-align: left middle;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refrescar", priority=True),
        Binding("q", "quit", "Salir", priority=True),
    ]

    # "todas" | "vivas" | "apagadas". Permite dedicar una pantalla a cada grupo
    # lanzando dos instancias: --filtro=vivas y --filtro=apagadas.
    FILTROS = ("todas", "vivas", "apagadas")

    def __init__(self, filtro: str = "todas", mitades: bool = False) -> None:
        super().__init__()
        self.filtro = filtro if filtro in self.FILTROS else "todas"
        # Pantalla partida en dos columnas. Nace de una limitación física: los dos
        # monitores del host cuelgan de la misma GPU y kmscon los espeja, así que
        # no se puede dar una vista a cada pantalla. Partir la única pantalla sí
        # consigue la separación visual. Solo tiene sentido viendo todo.
        self.mitades = mitades and self.filtro == "todas"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Tab / ↑↓ moverse  ·  Enter pulsar  ·  R refrescar  ·  Q salir  ·  (ratón también)", id="toolbar")
        if self.mitades:
            yield Horizontal(
                self._columna("mosaic-vivas", "● ENCENDIDAS", "on"),
                self._columna("mosaic-apagadas", "○ APAGADAS", "off"),
                id="split",
            )
        else:
            yield self._columna("mosaic")
        yield Static("", id="status-line")
        yield Footer()

    def _columna(self, grid_id: str, titulo: str = "", variante: str = "") -> Vertical:
        """Mosaico + su barra de flechas táctiles (y cabecera, en modo mitades)."""
        hijos = []
        if titulo:
            hijos.append(Static(titulo, classes=f"col-title {variante}"))
        hijos.append(VerticalScroll(id=grid_id))
        hijos.append(Horizontal(
            ScrollChip(grid_id, -1),
            ScrollChip(grid_id, +1),
            id=f"scroll-{grid_id}",
            classes="scrollbar-tactil",
        ))
        return Vertical(*hijos, classes="col")

    def on_mount(self) -> None:
        self.title = "Proxmox Panel"
        base = CONFIG.get("subtitulo") or socket.gethostname()
        etiqueta = {"vivas": "encendidas", "apagadas": "apagadas"}.get(self.filtro)
        self.sub_title = f"{base} · {etiqueta}" if etiqueta else base
        # Un registro de tarjetas y un orden por mosaico (en modo mitades hay dos)
        self._cards: dict[str, dict[int, MachineCard]] = {}
        self._orden: dict[str, list[int]] = {}
        self.refresh_list()
        self.set_interval(3.0, self.refresh_list)

    def refresh_list(self) -> None:
        try:
            machines = list_machines()
        except Exception as e:
            logging.exception("list_machines failed")
            self.set_status(f"error listando: {e}")
            return

        if self.filtro == "vivas":
            machines = [m for m in machines if m.is_running]
        elif self.filtro == "apagadas":
            machines = [m for m in machines if not m.is_running]

        if self.mitades:
            self._sync_grid("mosaic-vivas", [m for m in machines if m.is_running])
            self._sync_grid("mosaic-apagadas", [m for m in machines if not m.is_running])
        else:
            self._sync_grid("mosaic", machines)

        cts = [m for m in machines if m.kind == "ct"]
        vms = [m for m in machines if m.kind == "vm"]
        self.set_status(
            f"CT {sum(1 for c in cts if c.is_running)}/{len(cts)}  ·  "
            f"VM {sum(1 for v in vms if v.is_running)}/{len(vms)} encendidas"
        )

    def _sync_grid(self, grid_id: str, machines: list[Machine]) -> None:
        """Deja un mosaico con exactamente estas máquinas, en orden y actualizadas."""
        try:
            grid = self.query_one(f"#{grid_id}", VerticalScroll)
        except Exception:
            return

        cards = self._cards.setdefault(grid_id, {})
        current_ids = set(cards)
        new_ids = {m.vmid for m in machines}

        # En modo mitades, una máquina que arranca sale de un mosaico y entra en
        # el otro: esto lo resuelve solo, sin caso especial.
        for vid in current_ids - new_ids:
            cards.pop(vid).remove()

        by_id = {m.vmid: m for m in machines}
        for vid in current_ids & new_ids:
            cards[vid].update_machine(by_id[vid])

        # Encendidas primero y apagadas después; por vmid dentro de cada grupo.
        # En un grid no cabe un separador, así que el orden ES el agrupamiento.
        orden = sorted(machines, key=lambda x: (not x.is_running, x.vmid))

        for m in orden:
            if m.vmid not in cards:
                card = MachineCard(m)
                cards[m.vmid] = card
                grid.mount(card)

        # Reordenar el DOM solo cuando cambia el orden (al arrancar o parar una
        # máquina). Hacerlo en cada refresco de 3 s daría parpadeo y perdería el
        # foco sin motivo.
        ids = [m.vmid for m in orden]
        if ids != self._orden.get(grid_id):
            self._orden[grid_id] = ids
            for idx, vid in enumerate(ids):
                card = cards.get(vid)
                if card is None:
                    continue
                try:
                    if list(grid.children).index(card) != idx:
                        grid.move_child(card, before=idx)
                except Exception:
                    pass

        # El primer pase corre antes de que Textual mida los contenedores (en
        # modo mitades cada columna aún no sabe que mide la mitad), así que se
        # recalcula una vez el layout ya tiene tamaños reales.
        self._relayout(grid, len(machines))
        self.call_after_refresh(self._relayout, grid, len(machines))

        # machines ya viene filtrado, así que los contadores hablan de lo que se ve
        cts = [m for m in machines if m.kind == "ct"]
        vms = [m for m in machines if m.kind == "vm"]
        self.set_status(
            f"CT {sum(1 for c in cts if c.is_running)}/{len(cts)}  ·  "
            f"VM {sum(1 for v in vms if v.is_running)}/{len(vms)} encendidas"
        )

    # nº de líneas que consume una tarjeta (título 1 + 5 métricas [CPU/MEM/DISK/
    # GPU/NET] + botones 1 + borde 2 = 9)
    _CARD_MIN_H = 9
    # Ancho mínimo para que los 3 chips (min-width 9 + margen) quepan enteros.
    _CARD_MIN_W = 34
    # Ancho máximo útil: más allá de esto la tarjeta solo estira huecos (la barra
    # mide 12 celdas fijas). Evita que con 2-3 máquinas salga una columna única
    # de 150 celdas de ancho.
    _CARD_MAX_W = 60

    def _relayout(self, grid, n: int) -> None:
        """Columnas según el ANCHO disponible, no según lo que haga falta para
        que todo quepa de alto. Si las filas resultantes no caben, el mosaico
        hace scroll con las tarjetas a tamaño completo; antes se estrujaban las
        filas y se perdía la fila de botones."""
        if n <= 0:
            return

        # Medimos el mosaico en sí, no la pantalla: en modo mitades cada columna
        # tiene la mitad del ancho y hay que decidir por separado.
        ancho = grid.size.width or (self.size.width or 160)
        alto = grid.size.height or max(1, (self.size.height or 40) - 4)

        # Descontamos el padding lateral (2+2) y el hueco de la barra de scroll
        usable_w = max(self._CARD_MIN_W, ancho - 4 - 2)
        max_cols = max(1, min(4, usable_w // self._CARD_MIN_W, n))
        avail_h = max(1, alto)

        # Menos columnas = tarjetas más anchas (títulos sin cortar). Cogemos las
        # MÍNIMAS que sigan cabiendo de alto, así se llena la pantalla sin
        # estirar las filas. Si ninguna combinación cabe, usamos el máximo que
        # permita el ancho y el mosaico hace scroll.
        # En mitades, SIEMPRE una columna por lado. Cada mitad mide ~76 celdas:
        # justo una tarjeta cómoda. Partirla en dos sub-columnas de ~35 vuelve a
        # cortar nombres y valores absolutos, que es lo que se vino a arreglar;
        # aquí se prefiere scroll antes que apretar.
        if self.mitades:
            cols = 1
        else:
            cols = max_cols
            for c in range(1, max_cols + 1):
                if -(-n // c) * self._CARD_MIN_H <= avail_h:
                    cols = c
                    break

            # ...pero nunca tan pocas que las tarjetas queden desmesuradas de ancho
            cols = max(cols, min(max_cols, -(-usable_w // self._CARD_MAX_W)))

        if grid.styles.grid_size_columns != cols:
            grid.styles.grid_size_columns = cols

        # Las flechas solo estorban si no hay nada que desplazar.
        desborda = -(-n // cols) * self._CARD_MIN_H > avail_h
        try:
            self.query_one(f"#scroll-{grid.id}", Horizontal).set_class(desborda, "visible")
        except Exception:
            pass

    def on_resize(self, event) -> None:
        for grid_id, cards in (getattr(self, "_cards", None) or {}).items():
            try:
                self._relayout(self.query_one(f"#{grid_id}", VerticalScroll), len(cards))
            except Exception:
                pass

    def set_status(self, msg: str) -> None:
        try:
            self.query_one("#status-line", Static).update(msg)
        except Exception:
            pass

    def action_refresh(self) -> None:
        self.refresh_list()

    def _find_card(self, vmid: int) -> Optional[MachineCard]:
        """`_cards` está indexado por mosaico ({grid_id: {vmid: card}}), así que
        una tarjeta puede estar en cualquiera de ellos (en modo mitades hay dos)."""
        for cards in self._cards.values():
            card = cards.get(vmid)
            if card is not None:
                return card
        return None

    def on_scroll_chip_pressed(self, event: ScrollChip.Pressed) -> None:
        try:
            grid = self.query_one(f"#{event.grid_id}", VerticalScroll)
        except Exception:
            return
        if event.hacia < 0:
            grid.scroll_page_up()
        else:
            grid.scroll_page_down()

    def on_action_chip_pressed(self, event: ActionChip.Pressed) -> None:
        action, vmid = event.action, event.vmid
        logging.info("chip pressed action=%s vmid=%s", action, vmid)
        card = self._find_card(vmid)
        if card is None:
            logging.warning("chip de una tarjeta que ya no existe: vmid=%s", vmid)
            return
        kind = card.kind
        if action == "start":
            ok, err = machine_action(kind, "start", vmid)
            self.set_status(f"iniciar {kind} {vmid}: {'OK' if ok else err or 'fallo'}")
        elif action == "stop":
            ok, err = machine_action(kind, "shutdown", vmid)
            self.set_status(f"parar {kind} {vmid}: {'OK' if ok else err or 'fallo'}")
        elif action == "console":
            if kind == "ct":
                self.open_console(vmid)
            else:
                self.set_status(f"vm {vmid}: consola no disponible en tty (usa la web 8006)")
        self.set_timer(1.0, self.refresh_list)

    def open_console(self, vmid: int) -> None:
        """Consola del CT en el propio tty. Usa `lxc-console` directo (NO
        `pct console`, que envuelve en dtach persistente y no se cierra solo).
        Un hilo vigila el estado: si el contenedor se apaga, cierra la consola
        y vuelve al panel automáticamente."""
        import threading
        with self.suspend():
            print("\033[2J\033[H", end="", flush=True)
            print(f"  ╔═══ CONSOLA · CT {vmid} ═══════════════════════════════╗")
            print(f"  ║  Para VOLVER al panel:  pulsa  Ctrl-a  y luego  q     ║")
            print(f"  ║  (si apagas el contenedor, vuelve solo)              ║")
            print(f"  ╚══════════════════════════════════════════════════════╝")
            print(flush=True)
            proc = None
            try:
                proc = subprocess.Popen(["lxc-console", "-n", str(vmid)])
            except Exception as e:
                print(f"error abriendo consola: {e}")
                try: input("Enter para volver al panel...")
                except Exception: pass
                self.refresh_list()
                return

            stop = threading.Event()

            def _watch() -> None:
                # Si el CT deja de estar 'running', cerramos la consola → el
                # panel vuelve solo. wait() devuelve True al set() (salida normal).
                while not stop.wait(2.0):
                    try:
                        r = subprocess.run(
                            ["pct", "status", str(vmid)],
                            capture_output=True, text=True, timeout=8,
                            stdin=subprocess.DEVNULL)
                    except Exception:
                        continue
                    if "status: running" not in (r.stdout or ""):
                        try: proc.terminate()
                        except Exception: pass
                        return

            watcher = threading.Thread(target=_watch, daemon=True)
            watcher.start()
            try:
                proc.wait()
            except KeyboardInterrupt:
                try: proc.terminate()
                except Exception: pass
            finally:
                stop.set()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
        self.refresh_list()


def _parse_args(argv: list[str]) -> tuple[str, bool]:
    filtro, mitades = "todas", False
    for a in argv[1:]:
        if a.startswith("--filtro="):
            filtro = a.split("=", 1)[1].strip().lower()
        elif a == "--mitades":
            mitades = True
        elif a in ("-h", "--help"):
            print(__doc__)
            print("Uso: app.py [--filtro=todas|vivas|apagadas] [--mitades]")
            print("  --mitades   parte la pantalla: encendidas | apagadas")
            raise SystemExit(0)
    return filtro, mitades


if __name__ == "__main__":
    _filtro, _mitades = _parse_args(sys.argv)
    logging.basicConfig(
        filename=f"/var/log/lxc-panel{'' if _filtro == 'todas' else '-' + _filtro}.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("starting lxc-panel; TERM=%s stdin.isatty=%s stdout.isatty=%s",
                 __import__("os").environ.get("TERM"),
                 sys.stdin.isatty(), sys.stdout.isatty())
    try:
        LXCPanel(filtro=_filtro, mitades=_mitades).run()
    except Exception:
        logging.error("crashed:\n%s", traceback.format_exc())
        raise
    logging.info("exited cleanly")
