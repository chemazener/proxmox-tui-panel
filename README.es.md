[English](README.md) | **Español**

# Proxmox TUI Panel

Un panel de terminal a pantalla completa para Proxmox VE que lista todos los
contenedores y máquinas virtuales con CPU, memoria, disco, GPU y red en vivo, y
permite arrancarlos, pararlos o abrirles una consola. Corre **en el propio host**
—sin navegador, sin agente, sin túnel SSH— así que funciona en el monitor de la
máquina.

![Mosaico completo](docs/grid.png)

Está hecho con [Textual](https://textual.textualize.io/). Lee todo de una sola
llamada a `pvesh get /cluster/resources` y actúa mediante `pct` y `qm`.

## Para qué

La interfaz web de Proxmox está muy bien, pero necesita otro ordenador. Este
panel convierte la pantalla del propio hipervisor en un cuadro de mandos:
enchufas un monitor y ves qué está corriendo y puedes actuar, incluso con la red
caída.

## Qué hace

- **Mosaico adaptativo**: el número de columnas se deduce del ancho disponible y
  las filas mantienen siempre el alto completo de una tarjeta. Cuando no cabe
  todo, el mosaico **hace scroll** en vez de aplastar las tarjetas — así nunca
  se pierde la fila de botones.
- **Agrupado por estado**: primero las encendidas, después las apagadas,
  ordenadas por VMID dentro de cada grupo. El orden solo se recalcula cuando una
  máquina cambia de estado de verdad, así que no parpadea en cada refresco.
- **Métricas por máquina**: `⚙ CPU` · `▤ MEM` · `▦ DISK` · `◈ GPU` · `⇅ NET`,
  con barras sólidas que pasan a amarillo por encima del 50 % y a rojo por encima
  del 80 %, más los valores absolutos (vCPUs, GB usados/totales, tasas y totales
  de transferencia).
- **Entiende el passthrough de GPU**: las VMs que tienen una GPU por VFIO se
  muestran como `VFIO·<etiqueta>` en lugar de un 0 % que no significa nada.
- **Acciones**: iniciar, parar y, en contenedores arrancados, abrir consola.
- **Pantalla partida**: `--mitades` divide la pantalla en dos columnas,
  encendidas a la izquierda y apagadas a la derecha. `--filtro=vivas` y
  `--filtro=apagadas` limitan el panel a un solo grupo, si prefieres lanzar dos
  instancias.
- Ratón y teclado: `Tab` y flechas para moverse, `Enter` para pulsar, `r` para
  refrescar, `q` para salir. La rueda del ratón desplaza el mosaico.
- **Desplazamiento con el dedo**: cuando un mosaico desborda, aparece debajo una
  barra a todo lo ancho con `▲ subir` y `▼ bajar`. En un terminal no existe el
  gesto táctil —la aplicación solo recibe teclas y eventos de ratón—, así que un
  deslizamiento solo llega si el emulador lo traduce a rueda, y la Shell web de
  Proxmox no lo hace mientras la app tiene activado el seguimiento de ratón. Un
  **toque**, en cambio, sí llega como clic: por eso desde el móvil se desplaza
  pulsando.

### Scroll en lugar de recorte

En un terminal bajo, las tarjetas conservan su alto completo y el mosaico hace
scroll:

![Scroll en un terminal pequeño](docs/scroll.png)

### Pantalla partida

`--mitades` pone las encendidas a la izquierda y las apagadas a la derecha. Cada
mitad usa una única columna ancha, así los nombres y los valores absolutos siguen
legibles y lo que sobra se desplaza:

![Pantalla partida](docs/split.png)

### Una vista por estado

![Solo las máquinas encendidas](docs/running.png)

## Requisitos

- Proxmox VE (probado en 8.x y 9.x) — `pvesh`, `pct` y `qm` en el `PATH`
- Python 3.11 o superior
- [Textual](https://pypi.org/project/textual/) 8.x
- Opcional, para el monitor físico: [kmscon](https://github.com/Aetf/kmscon) con
  su módulo `mod-pango.so`, y una fuente monoespaciada con buena cobertura
  Unicode (DejaVu Sans Mono va bien)

## Instalación

```bash
git clone https://github.com/chemazener/proxmox-tui-panel.git
cd proxmox-tui-panel

install -d /opt/lxc-panel
install -m 644 app.py /opt/lxc-panel/app.py
install -m 755 panel /usr/local/bin/panel
python3 -m venv /opt/lxc-panel/venv
/opt/lxc-panel/venv/bin/pip install "textual==8.2.7"
```

En Debian puede que necesites `apt install python3-venv` antes: sin él el
entorno virtual se crea **sin `pip`** y la instalación de Textual falla con un
"No such file or directory" que no dice nada.

Ya se puede lanzar:

```bash
panel                    # todas las máquinas
panel --mitades          # partida: encendidas | apagadas
panel --filtro=vivas     # solo las encendidas
panel --filtro=apagadas  # solo las apagadas
```

`panel` es un lanzador de tres líneas; el servicio de systemd de abajo **no** lo
crea, así que instálalo aunque solo vayas a usar el servicio.

### En el monitor del host (opcional)

```bash
install -d /etc/lxc-panel
install -m 644 lxc-panel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lxc-panel.service
```

El unit envuelve el panel en kmscon sobre `tty1`, que es lo que da una fuente
decente y soporte de ratón en la consola. Lleva `Conflicts=getty@tty1.service`,
así que el panel sustituye al login de tty1.

kmscon no viene empaquetado en Debian; el [fork de Aetf](https://github.com/Aetf/kmscon)
funciona bien. Tres detalles que es fácil pasar por alto al instalarlo a mano: el
`kmscon` del `PATH` es un script envoltorio que ejecuta el binario real de
`libexec`, necesita `libtsm`, y necesita su módulo **`mod-pango.so`** (ver las
trampas más abajo).

### Que arranque al entrar (opcional)

Si además lo quieres en cada login interactivo de root, añade
[`profile-autostart.sh`](profile-autostart.sh) al final de `/root/.profile`. Lleva
guardas para que `ssh host "comando"`, `scp` y `rsync` no se vean afectados —
compruébalo tú después de instalarlo, porque romperlos sin darte cuenta es de lo
más incómodo de depurar:

```bash
time ssh root@tu-host 'echo ok'    # tiene que responder al instante
```

## Configuración

Todo lo propio de cada máquina vive en `/etc/lxc-panel/config.json`, que **no**
forma parte de este repositorio. Sin él el panel funciona igual, solo que no
puede etiquetar el passthrough de GPU. Copia `config.example.json` y ajústalo:

| Clave | Significado |
|---|---|
| `gpu_passthrough_pci` | `{"<prefijo-pci>": "<etiqueta>"}`. Toda VM cuya configuración tenga una línea `hostpci` que case con un prefijo se muestra como `VFIO·<primera letra de la etiqueta>`. Los prefijos salen de `lspci`. |
| `subtitulo` | Texto junto al título. Vacío = usa el hostname. |

```bash
install -m 600 config.example.json /etc/lxc-panel/config.json
```

## Notas y trampas

- **`--font-size` no hace nada y los iconos salen como cajas.** Significa que
  kmscon ha caído a su fuente bitmap interna `8x16` porque no pudo cargar
  `mod-pango.so`. Comprueba
  `journalctl -u lxc-panel | grep "font engine"`: tiene que decir `[pango]`. El
  módulo se carga con `dlopen`, así que un `ldd` sobre el binario de kmscon no
  delata la dependencia que falta.
- **`--font-size` puede ignorarse sin avisar.** La geometría del terminal sale de
  las métricas de celda de la fuente, así que menos tamaño = más columnas. En uno
  de mis hosts la opción funciona; en otro kmscon da siempre celdas de 8x16 sea
  cual sea el tamaño (probado con 16, 40 y con `--font-dpi` explícito), aunque
  diga `font engine [pango]`. Mide antes de dar nada por hecho:
  `python3 -c "import fcntl,struct,termios;f=open('/dev/tty1','rb');print(struct.unpack('HHHH',fcntl.ioctl(f,termios.TIOCGWINSZ,b'\0'*8))[:2])"`
- **Los emoji de color no se renderizan** en la consola. DejaVu Sans Mono no
  tiene glifos de emoji, así que el panel usa símbolos monocromos a propósito.
  Para comprobar un glifo candidato:
  `fc-query -f "%{charset}" /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf`.
- **Dos monitores no pueden mostrar vistas distintas bajo kmscon.** Un «seat»
  posee un dispositivo DRM completo, no conectores individuales, así que dos
  salidas de la misma GPU acaban espejadas. Repartir vistas entre pantallas
  físicas exige un compositor (sway y similares) que sepa colocar una ventana por
  salida — y aun así se quedaría en negro cuando esa GPU se cede a una VM.
  `--mitades` es la salida barata: parte la única pantalla que hay, y los dos
  monitores espejados muestran la misma división.
- **El panel no puede correr mientras la GPU que mueve la consola está pasada**
  por VFIO a una VM: el conflicto de DRM master deja la pantalla en negro.
- Las consolas se abren con `lxc-console`, no con `pct console`. Este último
  envuelve la sesión en un `dtach` persistente que no se cierra solo y deja el
  panel colgado.

## Previsualizar cambios sin tocar el monitor

Textual puede renderizar la aplicación headless, que es lo cómodo para revisar
cambios de maquetación:

```python
import asyncio, sys
sys.path.insert(0, "/opt/lxc-panel")
import app as A

A.list_machines = lambda: [...]          # datos de muestra, sin host en vivo

async def shot():
    app = A.LXCPanel()
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        open("out.svg", "w").write(app.export_screenshot())

asyncio.run(shot())
```

El SVG se convierte a PNG con cualquier navegador:
`chromium --headless --screenshot=out.png --window-size=2200,1300 out.svg`.

## Licencia

MIT — ver [LICENSE](LICENSE).
