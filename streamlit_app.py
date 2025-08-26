import streamlit as st
import zipfile, io, re, hashlib
from datetime import datetime

st.set_page_config(page_title="Bambu 3MF — Change Plates Duplicator", page_icon="🛠️", layout="centered")
st.title("Bambu 3MF — Change Plates Duplicator")
st.write("Sube un **.3mf**, define cuántas **repeticiones** quieres del mismo diseño y la app insertará entre cada repetición un bloque de **cambio de placa**. Se mantienen sólo una vez los códigos finales de apagado.")

# =========================
# Patrones / utilidades
# =========================

# Secciones "change plates"
SECTION_RE = re.compile(
    r"(;=+\s*Starting\s+to\s+change\s+plates[^\n]*\n)(.*?)(;=+\s*Finish\s+to\s+change\s+plates[^\n]*\n)",
    re.IGNORECASE | re.DOTALL,
)

# Ciclos Z: G380 S3 (down) / G380 S2 (up)
ZDOWN_RE = re.compile(r"^\s*G380\s+S3\s+Z-?\s*(?P<down>[0-9]+(?:\.[0-9]+)?)\s+F[0-9.]+\s*(?:;.*)?$", re.IGNORECASE)
ZUP_RE   = re.compile(r"^\s*G380\s+S2\s+Z\s*(?P<up>[0-9]+(?:\.[0-9]+)?)\s+F[0-9.]+\s*(?:;.*)?$", re.IGNORECASE)

# Heurística de “apagado final” (sólo al final de la última repetición)
SHUTDOWN_RE = re.compile(
    r"^\s*(M104\s+S0\b|M140\s+S0\b|M106\s+S0\b|M107\b|M84\b|M18\b)\s*.*$",
    re.IGNORECASE
)

DEFAULT_CHANGE_TEMPLATE = """;========Starting to change plates =================
G91;
{{CYCLES}}
G1 Z5 F1200
G90;
G28 Y;
G91;
G380 S2 Z30 F1200
G90;
M211 Y0 Z0;
G91;
G90;
G1 Y266 F2000;
G1 Y35 F1000
G1 Y0 F2500
G91;
G380 S3 Z-20 F1200
G90;
G1 Y266 F2000
G1 Y53 F2000
G1 Y266 F2000
G1 Y250 F8000
G1 Y266 F8000
G1 Y100 F2000
G1 Y266 F2000
G1 Y250 F8000
G1 Y266 F8000
G1 Y0 F1000
G1 Y150 F1000
G28 Y;
;========Finish to change plates =================
"""

def md5_bytes(b:bytes):
    h = hashlib.md5(); h.update(b); return h.hexdigest()

def find_cycles(lines):
    """Encuentra bloque contiguo de pares ZDOWN/ZUP. Devuelve (start, end, [(down, up), ...])."""
    i = 0
    while i < len(lines):
        if ZDOWN_RE.match(lines[i] or ""):
            break
        i += 1
    start = i
    cycles = []
    while i + 1 < len(lines):
        m_down = ZDOWN_RE.match(lines[i] or "")
        m_up   = ZUP_RE.match(lines[i+1] or "")
        if not (m_down and m_up):
            break
        down = float(m_down.group("down"))
        up   = float(m_up.group("up"))
        cycles.append((down, up))
        i += 2
    end = i
    if cycles:
        return start, end, cycles
    return None, None, []

def rebuild_cycles(desired_cycles:int, down_mm:float, up_mm:float, example_down_line:str=None, example_up_line:str=None):
    """Reconstruye N ciclos. Si hay líneas de ejemplo, hereda feedrates/comentarios; si no, usa F1200."""
    def extract_F(line, default=" F1200"):
        if not line:
            return default
        m = re.search(r"\sF([0-9.]+)", line, re.IGNORECASE)
        return f" F{m.group(1)}" if m else default

    f_down = extract_F(example_down_line)
    f_up   = extract_F(example_up_line)

    comment_down = ""
    if example_down_line:
        mcd = re.search(r"(;.*)$", example_down_line)
        if mcd: comment_down = " " + mcd.group(1).lstrip()

    comment_up = ""
    if example_up_line:
        mcu = re.search(r"(;.*)$", example_up_line)
        if mcu: comment_up = " " + mcu.group(1).lstrip()

    out = []
    for _ in range(desired_cycles):
        out.append(f"G380 S3 Z-{down_mm}{f_down}{(' ' + comment_down) if comment_down and not comment_down.startswith(';') else comment_down}".rstrip() + "\n")
        out.append(f"G380 S2 Z{up_mm}{f_up}{(' ' + comment_up) if comment_up and not comment_up.startswith(';') else comment_up}".rstrip() + "\n")
    return "".join(out)

def normalize_existing_change_sections(text, cycles:int, down_mm:float, up_mm:float, report:list):
    """Si hay secciones change plates, ajusta los ciclos/mm; devuelve texto y, si existe, la PRIMERA sección normalizada."""
    first_section_text = None
    changed_any = False

    def _replace(m):
        nonlocal first_section_text, changed_any
        head, body, tail = m.group(1), m.group(2), m.group(3)
        lines = body.splitlines(keepends=True)
        s, e, found = find_cycles([ln.rstrip('\n') for ln in lines])
        if found:
            example_down = lines[s].rstrip("\n")
            example_up   = lines[s+1].rstrip("\n")
            new_cycle_lines = rebuild_cycles(cycles, down_mm, up_mm, example_down, example_up)
            new_body = "".join(lines[:s]) + new_cycle_lines + "".join(lines[e:])
            res = head + new_body + tail
            if first_section_text is None:
                first_section_text = res
            changed_any = True
            report.append(f"[change plates] ciclos {len(found)} -> {cycles} (down={down_mm}, up={up_mm})")
            return res
        else:
            # Si no hay ciclos, dejamos tal cual
            if first_section_text is None:
                first_section_text = head + body + tail
            report.append("[change plates] sección sin ciclos detectables; no se modifica.")
            return head + body + tail

    new_text, n = SECTION_RE.subn(_replace, text)
    if n == 0:
        report.append("No se encontraron secciones 'change plates'.")
        return text, None, False
    return new_text, first_section_text, changed_any

def build_change_block_from_template(cycles:int, down_mm:float, up_mm:float, template:str):
    """Genera un bloque completo a partir de plantilla con {{CYCLES}}."""
    cycle_lines = rebuild_cycles(cycles, down_mm, up_mm, None, None)
    if "{{CYCLES}}" in template:
        return template.replace("{{CYCLES}}", cycle_lines)
    # Si el usuario no puso placeholder, insertamos tras la segunda línea
    parts = template.splitlines(keepends=True)
    inject_at = min(2, len(parts))
    return "".join(parts[:inject_at]) + cycle_lines + "".join(parts[inject_at:])

def split_core_and_shutdown(text:str):
    """Separa core (lo que se repite) y shutdown final (una sola vez)."""
    lines = text.splitlines(keepends=True)
    # Buscar desde el final la primera línea que parezca de apagado
    idx = None
    for i in range(len(lines)-1, -1, -1):
        if SHUTDOWN_RE.match(lines[i]):
            idx = i
            break
    if idx is None:
        # Si no hay apagado claro, repetimos todo como core y sin shutdown
        return text, ""
    # Incluimos todo desde idx hasta el final como shutdown
    core = "".join(lines[:idx])
    shutdown = "".join(lines[idx:])
    return core, shutdown

def duplicate_with_change_blocks(gcode_text:str, repeats:int, change_block:str, report:list):
    """Compone: core + (change + core)*(repeats-1) + shutdown."""
    core, shutdown = split_core_and_shutdown(gcode_text)
    if repeats <= 1:
        return core + shutdown
    out = [core]
    for _ in range(repeats - 1):
        out.append("\n")
        out.append(change_block)
        out.append("\n")
        out.append(core)
    out.append(shutdown)
    report.append(f"Duplicación: {repeats} repeticiones; bloque change plates insertado {repeats-1} veces.")
    return "".join(out)

def process_one_gcode(gcode_bytes:bytes, repeats:int, cycles:int, down_mm:float, up_mm:float, user_tpl:str, use_existing_tpl:bool, report:list):
    """Procesa un plate_*.gcode completo."""
    text = gcode_bytes.decode("utf-8", errors="ignore")

    # 1) Normalizar/ajustar secciones existentes, y tomar primera como plantilla si se pide
    norm_text, existing_block, _ = normalize_existing_change_sections(text, cycles, down_mm, up_mm, report)

    # 2) Elegir plantilla a usar
    if use_existing_tpl and existing_block:
        change_block = existing_block
        report.append("Plantilla change plates: se usó la primera sección existente (normalizada).")
    else:
        change_block = build_change_block_from_template(cycles, down_mm, up_mm, user_tpl or DEFAULT_CHANGE_TEMPLATE)
        report.append("Plantilla change plates: se usó la plantilla definida (o por defecto).")

    # 3) Duplicar con bloques de cambio entre repeticiones
    duplicated = duplicate_with_change_blocks(norm_text, repeats, change_block, report)
    return duplicated.encode("utf-8")

def process_3mf(src_bytes: bytes, repeats:int, cycles:int, down_mm:float, up_mm:float, user_tpl:str, use_existing_tpl:bool):
    """Carga 3MF, procesa todos los Metadata/plate_*.gcode, recalcula .md5 y agrega reporte."""
    in_mem = io.BytesIO(src_bytes)
    zin = zipfile.ZipFile(in_mem, "r")

    out_mem = io.BytesIO()
    zout = zipfile.ZipFile(out_mem, "w", compression=zipfile.ZIP_DEFLATED)

    report = []
    modified_files = 0

    for info in zin.infolist():
        data = zin.read(info.filename)
        lower = info.filename.lower()
        if lower.startswith("metadata/") and lower.endswith(".gcode"):
            new_data = process_one_gcode(data, repeats, cycles, down_mm, up_mm, user_tpl, use_existing_tpl, report)
            zout.writestr(info, new_data)
            modified_files += 1
        else:
            zout.writestr(info, data)

    zout.close()
    zin.close()

    # Reabrir, recomputar MD5 si existen archivos .md5
    in2 = io.BytesIO(out_mem.getvalue())
    ztmp = zipfile.ZipFile(in2, "r")
    out_final = io.BytesIO()
    zfinal = zipfile.ZipFile(out_final, "w", compression=zipfile.ZIP_DEFLATED)

    file_cache = {info.filename: ztmp.read(info.filename) for info in ztmp.infolist()}
    ztmp.close()

    for name in list(file_cache.keys()):
        if name.lower().startswith("metadata/plate_") and name.lower().endswith(".gcode.md5"):
            gcode_name = name[:-4]
            if gcode_name in file_cache:
                digest = md5_bytes(file_cache[gcode_name]) + "\n"
                file_cache[name] = digest.encode("ascii")

    for name, data in file_cache.items():
        zfinal.writestr(name, data)

    ts = datetime.utcnow().isoformat() + "Z"
    rpt = [f"# Reporte ({ts})",
           f"- GCODEs procesados: {modified_files}",
           f"- Repeticiones: {repeats}",
           f"- Ciclos: {cycles} | down={down_mm} | up={up_mm}"]
    rpt.extend([f"- {r}" for r in report])
    zfinal.writestr("Metadata/change_plates_report.txt", ("\n".join(rpt) + "\n").encode("utf-8"))
    zfinal.close()
    out_final.seek(0)
    return out_final.getvalue(), modified_files, report

# =========================
# UI
# =========================

st.subheader("Parámetros")
col1, col2, col3, col4 = st.columns(4)
with col1:
    repeats = st.number_input("Repeticiones totales", min_value=1, value=2, step=1)
with col2:
    cycles = st.number_input("Ciclos Z (por cambio)", min_value=0, value=4, step=1)
with col3:
    down_mm = st.number_input("Descenso Z (mm)", min_value=0.0, value=20.0, step=0.5, format="%.1f")
with col4:
    up_mm = st.number_input("Ascenso Z (mm)", min_value=0.0, value=75.0, step=0.5, format="%.1f")

use_existing_tpl = st.checkbox("Usar como plantilla la PRIMERA sección 'change plates' existente (si hay)", value=True)

with st.expander("Plantilla de 'change plates' (opcional)"):
    st.caption("Si hay una sección existente y activaste la casilla anterior, se usará esa como plantilla. Si no, se usará esta. Usa {{CYCLES}} donde quieras inyectar los ciclos Z.")
    user_template = st.text_area("Plantilla", value=DEFAULT_CHANGE_TEMPLATE, height=260)

uploaded = st.file_uploader("Archivo .3mf", type=["3mf"])

if uploaded is not None:
    st.info(f"Archivo: **{uploaded.name}** ({uploaded.size/1024:.1f} KB)")
    if st.button("Procesar 3MF"):
        try:
            result_bytes, modified, _ = process_3mf(uploaded.read(), int(repeats), int(cycles), float(down_mm), float(up_mm), user_template, use_existing_tpl)
            st.success(f"OK. GCODEs modificados: {modified}.")
            st.download_button(
                label="Descargar 3MF modificado",
                data=result_bytes,
                file_name=f"modified_{uploaded.name}",
                mime="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
            )
        except Exception as e:
            st.error(f"Error: {e}")
else:
    st.caption("Sube un archivo para habilitar el procesamiento.")
