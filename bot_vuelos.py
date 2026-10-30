"""Bot de precios: vuelos directos Bogotá (BOG) -> Orlando (MCO), diciembre 2026.

Revisa en Google Flights varias combinaciones de fechas (mínimo 4 noches),
solo vuelos directos, sin Frontier, precios en COP para 4 adultos.
Solo manda correo cuando el precio es realmente bueno:
  - el precio por persona baja del UMBRAL_POR_PERSONA, o
  - aparece un mínimo nuevo al menos BAJADA_FUERTE más barato que el mínimo visto.
Además manda un resumen semanal (lunes) para que sepas que sigue vivo.

Uso:
    python3 bot_vuelos.py              # revisa y avisa si hay algo bueno
    python3 bot_vuelos.py --dry-run    # revisa e imprime, sin correo ni guardar
    python3 bot_vuelos.py --resumen    # fuerza el correo de resumen

En GitHub Actions corre cada 3 horas (ver .github/workflows/bot.yml) y avisa por Gmail.
"""

import json
import os
import random
import re
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

# ---------------- Configuración ----------------
# "recordatorios" (suena en el iPhone vía iCloud, solo desde el Mac), "gmail" (desde la nube) o "correo" (app Mail)
AVISAR_POR = os.environ.get("AVISAR_POR", "recordatorios")
EN_LA_NUBE = os.environ.get("GITHUB_ACTIONS") == "true"
LISTA_RECORDATORIOS = "Vuelos Orlando"
EMAIL_TO = "andresxnz@outlook.com"
PASAJEROS = 4  # 3 adultos + joven de 17 (paga tarifa de adulto)
FECHAS_IDEALES = (date(2026, 12, 22), date(2026, 12, 27))  # las fechas que de verdad quieren
FLEXIBILIDAD_DIAS = 2  # fechas cercanas: hasta 2 días antes o después, en la ida y en la vuelta
NOCHES_MIN, NOCHES_MAX = 4, 7
EXCLUIR = ["frontier"]
UMBRAL_POR_PERSONA = 2_100_000  # COP ida y vuelta por persona = "precio bueno"
BAJADA_FUERTE = 0.05  # 5% por debajo del mínimo visto también cuenta como bueno
MIN_REVISIONES_PARA_BAJADA = 4  # no comparar contra un historial muy corto
REALERTA_SI_BAJA = 0.02  # tras avisar, solo vuelve a avisar si baja otro 2%
# ------------------------------------------------

# Primero tus fechas, después las cercanas.
COMBINACIONES = sorted(
    (
        (FECHAS_IDEALES[0] + timedelta(days=ds), FECHAS_IDEALES[1] + timedelta(days=dr))
        for ds in range(-FLEXIBILIDAD_DIAS, FLEXIBILIDAD_DIAS + 1)
        for dr in range(-FLEXIBILIDAD_DIAS, FLEXIBILIDAD_DIAS + 1)
        if NOCHES_MIN <= (FECHAS_IDEALES[1] - FECHAS_IDEALES[0]).days + dr - ds <= NOCHES_MAX
    ),
    key=lambda c: (c != FECHAS_IDEALES, c),
)
# Se revisa hasta el día antes de la última salida posible; las salidas que ya pasaron se saltan.
ULTIMO_DIA_REVISION = max(salida for salida, _ in COMBINACIONES) - timedelta(days=1)

CARPETA = Path(__file__).resolve().parent
ESTADO = CARPETA / "estado.json"

ETIQUETA = re.compile(
    r"From ([\d,]+) Colombian pesos round trip total\."
    r"(?P<basica>This price does not include overhead bin access\. )?"
    r"\s*Nonstop flight with (?P<aerolinea>.+?)\."
    r"(?: Operated by (?P<opera>.+?)\.)?"
    r" Leaves .+? at (?P<hora>\d{1,2}:\d{2}\s?[AP]M)"
)
INSIGHT = re.compile(r"Prices are currently (low|typical|high)")
INSIGHT_ES = {"low": "bajos", "typical": "normales", "high": "altos"}
RESERVAR = {
    "avianca": "https://www.avianca.com/es/",
    "latam": "https://www.latamairlines.com/co/es",
}
SEL_VUELO = '[aria-label*="round trip total"]'


def url_busqueda(salida, regreso):
    q = (
        f"Nonstop flights to MCO from BOG on {salida.isoformat()} "
        f"through {regreso.isoformat()} for {PASAJEROS} adults"
    )
    return f"https://www.google.com/travel/flights?q={quote(q)}&curr=COP&hl=en&gl=CO"


def cop(n):
    return "$" + f"{n:,}".replace(",", ".")


def fecha_corta(iso):
    return f"{date.fromisoformat(iso).day} dic"


def buscar(page, salida, regreso):
    """Devuelve (vuelos, insight_google, bloqueado)."""
    for intento in range(3):  # Google a veces tarda, sobre todo al despertar el Mac
        try:
            page.goto(url_busqueda(salida, regreso), wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as e:
            print(f"  intento {intento + 1} falló: {type(e).__name__}")
            page.wait_for_timeout(15000)
    else:
        return [], None, False
    if "/sorry/" in page.url or "unusual traffic" in page.content().lower():
        return [], None, True
    try:
        page.wait_for_selector('[aria-label*="round trip total"]', timeout=30000)
    except Exception:
        return [], None, False
    page.wait_for_timeout(2500)  # dejar que termine de cargar la lista
    etiquetas = page.eval_on_selector_all(
        '[aria-label*="round trip total"]',
        "els => els.map(e => e.getAttribute('aria-label'))",
    )
    m_ins = INSIGHT.search(page.inner_text("body"))
    insight = m_ins.group(1) if m_ins else None

    vuelos, vistos = [], set()
    for et in etiquetas:
        m = ETIQUETA.search(et or "")
        if not m:
            continue
        aerolinea = m.group("aerolinea")
        opera = m.group("opera") or ""
        if any(x in (aerolinea + opera).lower() for x in EXCLUIR):
            continue
        clave = (aerolinea, m.group("hora"))
        if clave in vistos:
            continue
        vistos.add(clave)
        vuelos.append({
            "salida": salida.isoformat(),
            "regreso": regreso.isoformat(),
            "noches": (regreso - salida).days,
            "aerolinea": aerolinea,
            "hora": m.group("hora"),
            "total": int(m.group(1).replace(",", "")),
            "basica": bool(m.group("basica")),
            "insight": insight,
            "url": url_busqueda(salida, regreso),
        })
    return vuelos, insight, False


def link_latam(v):
    return (
        "https://www.latamairlines.com/co/es/ofertas-vuelos?origin=BOG&destination=MCO"
        f"&outbound={v['salida']}T12%3A00%3A00.000Z&inbound={v['regreso']}T12%3A00%3A00.000Z"
        f"&adt={PASAJEROS}&chd=0&inf=0&trip=RT&cabin=Economy&redemption=false&sort=RECOMMENDED"
    )


def es_ideal(v):
    return (v["salida"], v["regreso"]) == (FECHAS_IDEALES[0].isoformat(), FECHAS_IDEALES[1].isoformat())


def opciones_para_reservar(vuelos):
    """La mejor de tus fechas, las más baratas y, si hace falta, otra aerolínea (máx. 3)."""
    elegidos = [v for v in vuelos if es_ideal(v)][:1]
    libres = 3 if elegidos else 2  # con tus fechas fijas, las otras 2 son las más baratas
    for v in vuelos:
        if v in elegidos:
            continue
        if len(elegidos) < libres or v["aerolinea"] not in {e["aerolinea"] for e in elegidos}:
            elegidos.append(v)
        if len(elegidos) == 3:
            break
    return elegidos


def completar_reserva(page, v):
    """Pone en v el link de la página de reserva de Google (botón "Reservar con
    <aerolínea>" que lleva a su web oficial) y el precio real que da la aerolínea ahí."""
    v["reserva"] = link_reserva_google(page, v)
    v["precio_real"] = None
    if not v["reserva"]:
        return
    patron = re.compile(rf"Book with {re.escape(v['aerolinea'])}\s*Airline\s*COP\s*([\d,]+)")
    for _ in range(12):  # los precios de la aerolínea tardan unos segundos en cargar
        m = patron.search(page.inner_text("body"))
        if m:
            v["precio_real"] = int(m.group(1).replace(",", ""))
            return
        page.wait_for_timeout(2500)


def link_reserva_google(page, v):
    """Elige ida y vuelta en Google Flights y devuelve el link de la página de reserva."""
    try:
        page.goto(v["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(SEL_VUELO, timeout=30000)
        page.wait_for_timeout(2500)
        page.locator(
            f'{SEL_VUELO}[aria-label*="Nonstop flight with {v["aerolinea"]}"][aria-label*="at {v["hora"]}"]'
        ).first.evaluate("e => e.click()")  # el clic normal lo tapa otro elemento
        page.wait_for_selector(f'{SEL_VUELO}[aria-label*="Leaves Orlando"]', timeout=30000)
        page.wait_for_timeout(2500)
        etiquetas = page.eval_on_selector_all(SEL_VUELO, "els => els.map(e => e.getAttribute('aria-label'))")
        regresos = [
            (int(m.group(1).replace(",", "")), i)
            for i, et in enumerate(etiquetas)
            if (m := ETIQUETA.search(et or "")) and m.group("aerolinea") == v["aerolinea"]
            and "Leaves Orlando" in et
        ]
        if not regresos:
            return None
        page.locator(SEL_VUELO).nth(min(regresos)[1]).evaluate("e => e.click()")
        page.wait_for_url("**/travel/flights/booking**", timeout=30000)
        return page.url.replace("hl=en", "hl=es")
    except Exception as e:
        print(f"  no se pudo sacar link de reserva ({v['aerolinea']}): {type(e).__name__}")
        return None


def motivo_umbral(total):
    """Motivo si el precio total (para los 4) queda debajo del umbral por persona, o None."""
    por_persona = total // PASAJEROS
    if por_persona <= UMBRAL_POR_PERSONA:
        return f"{cop(por_persona)} por persona, debajo de tu umbral de {cop(UMBRAL_POR_PERSONA)}"
    return None


def motivo_bajada(total_desde, estado):
    """Motivo si el precio "desde" cae fuerte contra el historial, que también guarda precios "desde"."""
    revisiones = estado["revisiones"]
    if len(revisiones) >= MIN_REVISIONES_PARA_BAJADA:
        minimo = min(r["mejor"] for r in revisiones)
        if total_desde <= minimo * (1 - BAJADA_FUERTE):
            pct = round((1 - total_desde / minimo) * 100)
            return f"{pct}% más barato que el mínimo visto ({cop(minimo)})"
    return None


def detalle(vuelos, estado):
    vuelos = sorted(vuelos, key=lambda v: v["total"])
    mejor = vuelos[0]
    lineas = [
        "Vuelos DIRECTOS Bogotá → Orlando · 4 adultos · ida y vuelta · sin Frontier",
        f"Revisado: {datetime.now():%d/%m/%Y %H:%M}",
        "",
        f"MEJOR PRECIO: {cop(mejor['total'])} COP en total "
        f"({cop(mejor['total'] // PASAJEROS)} por persona)",
        f"  {mejor['aerolinea']} · sale {fecha_corta(mejor['salida'])} {mejor['hora']} · "
        f"regresa {fecha_corta(mejor['regreso'])} · {mejor['noches']} noches"
        + (" · tarifa básica, sin maleta de mano" if mejor["basica"] else ""),
    ]
    if mejor["insight"]:
        lineas.append(f"  Google dice que los precios para esas fechas están {INSIGHT_ES[mejor['insight']]}")
    if estado["revisiones"]:
        minimo = min(r["mejor"] for r in estado["revisiones"])
        lineas.append(f"  Mínimo visto desde que empezó el bot: {cop(min(minimo, mejor['total']))}")

    lineas += ["", "TOP 10 OPCIONES", "-" * 60]
    for i, v in enumerate(vuelos[:10], 1):
        nota = " (básica, sin maleta de mano)" if v["basica"] else ""
        lineas.append(
            f"{i:>2}. {cop(v['total'])} · {v['aerolinea']} · "
            f"{fecha_corta(v['salida'])} → {fecha_corta(v['regreso'])} "
            f"({v['noches']} noches) · sale {v['hora']}{nota}"
        )

    lineas += ["", "RESERVAR YA (web oficial de la aerolínea)", "-" * 60]
    for i, v in enumerate(opciones_para_reservar(vuelos), 1):
        lineas.append(
            f"{i}. {v['aerolinea']} · {fecha_corta(v['salida'])} → {fecha_corta(v['regreso'])} · "
            f"sale {v['hora']} · {cop(v['total'])}"
        )
        if v.get("precio_real"):
            lineas.append(
                f"   Precio real en {v['aerolinea']}: {cop(v['precio_real'])} "
                f"({cop(v['precio_real'] // PASAJEROS)} por persona)"
            )
        if v.get("reserva"):
            lineas.append(f"   Vuelo exacto ya elegido → botón \"Reservar con {v['aerolinea']}\": {v['reserva']}")
        else:
            lineas.append(f"   Ver en Google Flights: {v['url']}")
        if "latam" in v["aerolinea"].lower():
            lineas.append(f"   Buscar en latam.com (fechas y 4 pasajeros ya puestos): {link_latam(v)}")
        elif "avianca" in v["aerolinea"].lower():
            lineas.append(f"   avianca.com (ahí toca poner fechas y pasajeros): {RESERVAR['avianca']}")
    lineas += [
        "",
        "Compra en la web oficial de la aerolínea. Desconfía de agencias desconocidas",
        "que ofrecen precios mucho más bajos que estos.",
        "",
        "Precios totales para los 4, con impuestos. Las maletas facturadas van aparte.",
    ]
    return mejor, "\n".join(lineas)


def correos_atascados():
    r = subprocess.run(
        ["osascript", "-e", 'tell application "Mail" to return count of messages of outbox'],
        capture_output=True, text=True,
    )
    return int(r.stdout.strip() or 0)


def enviar_correo(asunto, cuerpo):
    """Envía por la app Mail y confirma que el correo de verdad salió de la Bandeja de salida."""
    if correos_atascados():
        # Mail no está enviando (p. ej. la cuenta perdió la sesión); no apilar más correos.
        print("Mail tiene correos atascados en la Bandeja de salida; no se envía.")
        return False
    script = '''
on run argv
    tell application "Mail"
        set m to make new outgoing message with properties ¬
            {subject:(item 1 of argv), content:(item 2 of argv), visible:false}
        tell m to make new to recipient at end of to recipients ¬
            with properties {address:(item 3 of argv)}
        send m
    end tell
end run
'''
    subprocess.run(["osascript", "-e", script, asunto, cuerpo, EMAIL_TO], check=True)
    for _ in range(12):
        time.sleep(10)
        if not correos_atascados():
            return True
    print("El correo quedó atascado en la Bandeja de salida de Mail.")
    return False


def crear_recordatorio(titulo, notas):
    """Crea un recordatorio en iCloud que suena en el iPhone en un minuto."""
    script = '''
on run argv
    tell application "Reminders"
        if not (exists list (item 3 of argv)) then make new list with properties {name:(item 3 of argv)}
        set d to (current date) + 60
        tell list (item 3 of argv)
            make new reminder with properties ¬
                {name:(item 1 of argv), body:(item 2 of argv), remind me date:d, due date:d, priority:1}
        end tell
    end tell
end run
'''
    r = subprocess.run(
        ["osascript", "-e", script, titulo, notas, LISTA_RECORDATORIOS], capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"No se pudo crear el recordatorio: {r.stderr.strip()}")
    return r.returncode == 0


def texto_recordatorio(vuelos, motivo=None):
    """Versión corta para leer en el celular: precio real y links para reservar."""
    opciones = sorted(
        opciones_para_reservar(sorted(vuelos, key=lambda v: v["total"])),
        key=lambda v: v.get("precio_real") or v["total"],
    )
    lineas = [f"Por qué te aviso: {motivo}.", ""] if motivo else []
    for i, v in enumerate(opciones, 1):
        precio = v.get("precio_real") or v["total"]
        etiqueta = "precio real" if v.get("precio_real") else "desde"
        lineas += [
            f"{i}. {'⭐ TUS FECHAS · ' if es_ideal(v) else ''}{v['aerolinea']} · "
            f"{fecha_corta(v['salida'])} → {fecha_corta(v['regreso'])} "
            f"({v['noches']} noches) · sale {v['hora']}",
            f"   {cop(precio)} para los 4 ({etiqueta}) · {cop(precio // PASAJEROS)} c/u",
            f"   Reservar: {v.get('reserva') or v['url']}",
            "",
        ]
    lineas += [
        f"Tu umbral: {cop(UMBRAL_POR_PERSONA)} por persona.",
        "Abre el link y dale a \"Reservar con Avianca/LATAM\" para comprar en la web oficial.",
        "Precios con impuestos; maletas facturadas aparte.",
    ]
    return "\n".join(lineas)


def enviar_gmail(asunto, cuerpo):
    """Envía desde tu Gmail a ti mismo con una contraseña de aplicación (secretos de GitHub)."""
    usuario = os.environ.get("GMAIL_USER", "").strip()
    clave = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()  # Google la muestra con espacios
    if "@" not in usuario or len(clave) != 16:
        print(
            f"Revisa los secretos: GMAIL_USER {'parece un correo' if '@' in usuario else 'NO parece un correo'}, "
            f"GMAIL_APP_PASSWORD tiene {len(clave)} caracteres sin espacios (deben ser 16)."
        )
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = asunto, usuario, os.environ.get("EMAIL_TO") or usuario
    msg.set_content(cuerpo)
    for puerto in (465, 587):  # si un puerto falla, se prueba el otro
        paso = "conectar"
        try:
            if puerto == 465:
                s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
            else:
                s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
                s.starttls()
            with s:
                paso = "iniciar sesión"
                s.login(usuario, clave)
                paso = "enviar"
                s.send_message(msg)
            return True
        except Exception as e:
            print(f"Gmail (puerto {puerto}) falló al {paso}: {type(e).__name__}: {e}")
    return False


def avisar(asunto, cuerpo_correo, texto_corto):
    if AVISAR_POR == "recordatorios":
        return crear_recordatorio(asunto, texto_corto)
    if AVISAR_POR == "gmail":
        return enviar_gmail(asunto, f"{texto_corto}\n\n{'-' * 60}\nDETALLE COMPLETO\n\n{cuerpo_correo}")
    return enviar_correo(asunto, cuerpo_correo)


def abrir_navegador(p):
    # En la nube se usa el Chromium que instala Playwright; en el Mac, tu Google Chrome.
    browser = p.chromium.launch(channel="chromium" if EN_LA_NUBE else "chrome", headless=True)
    return browser, browser.new_page(locale="en-US", timezone_id="America/Bogota")


def notificar_mac(titulo, mensaje):
    if EN_LA_NUBE:
        return
    subprocess.run([
        "osascript", "-e", "on run argv",
        "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
        "-e", "end run", titulo, mensaje,
    ])


def probar_aviso():
    """Busca un par de fechas y manda un aviso de prueba con precios y links reales."""
    vuelos = []
    with sync_playwright() as p:
        browser, page = abrir_navegador(p)
        for salida, regreso in [FECHAS_IDEALES, (date(2026, 12, 23), date(2026, 12, 28))]:
            vuelos += buscar(page, salida, regreso)[0]
        vuelos.sort(key=lambda v: v["total"])
        for v in opciones_para_reservar(vuelos):
            completar_reserva(page, v)
        browser.close()
    if not vuelos:
        return avisar("🧪 Prueba bot de vuelos", "", "Google no devolvió vuelos ahora, pero los avisos funcionan.")
    texto = "Esto es una PRUEBA del bot (solo 2 fechas revisadas).\n\n" + texto_recordatorio(vuelos)
    print(texto)
    return avisar(f"🧪 Prueba bot vuelos BOG→MCO: desde {cop(vuelos[0]['total'])}", texto, texto)


def main():
    if "--probar-aviso" in sys.argv:
        ok = probar_aviso()
        print("Aviso de prueba enviado" if ok else "El aviso de prueba falló")
        sys.exit(0 if ok else 1)
    dry_run = "--dry-run" in sys.argv
    forzar_resumen = "--resumen" in sys.argv
    ahora = datetime.now()
    hoy = ahora.date().isoformat()
    if ahora.date() > ULTIMO_DIA_REVISION:
        print("El viaje ya pasó; no hay nada que revisar.")
        return

    estado = json.loads(ESTADO.read_text()) if ESTADO.exists() else {}
    estado.setdefault("revisiones", [])
    estado.setdefault("ultima_alerta", None)
    estado.setdefault("ultimo_resumen", None)
    estado.setdefault("ultimo_aviso_error", None)
    estado.setdefault("ultimo_aviso_mail", None)

    print(f"\n=== Revisión {ahora:%d/%m/%Y %H:%M} ===")
    vuelos, bloqueado = [], False
    with sync_playwright() as p:
        browser, page = abrir_navegador(p)
        for salida, regreso in COMBINACIONES:
            if salida <= ahora.date():
                continue
            try:
                encontrados, _, bloqueado = buscar(page, salida, regreso)
            except Exception as e:  # una fecha que falla no tumba toda la revisión
                print(f"{salida} → {regreso}: error {type(e).__name__}")
                continue
            if bloqueado:
                break
            print(f"{salida} → {regreso}: {len(encontrados)} vuelos")
            vuelos += encontrados
            page.wait_for_timeout(random.randint(2000, 6000))  # ir despacio

        alerta = resumen = confirmado = False
        motivo = precio_alerta = None
        if vuelos:
            vuelos.sort(key=lambda v: v["total"])
            mejor = vuelos[0]
            ultima = estado["ultima_alerta"]
            # Filtro rápido con el precio "desde" (casi siempre igual o menor que el real):
            # si ni así vale la pena, no hace falta abrir las páginas de reserva.
            motivo = motivo_umbral(mejor["total"]) or motivo_bajada(mejor["total"], estado)
            alerta = bool(motivo) and (ultima is None or mejor["total"] <= ultima * (1 - REALERTA_SI_BAJA))
            resumen = forzar_resumen or (
                ahora.weekday() == 0 and ahora.hour >= 8 and estado["ultimo_resumen"] != hoy
            )
            # Los links de reserva solo se sacan si se va a mandar correo.
            if alerta or resumen:
                opciones = opciones_para_reservar(vuelos)
                for v in opciones:
                    completar_reserva(page, v)
                reales = [v["precio_real"] for v in opciones if v.get("precio_real")]
                confirmado = bool(reales)
                precio_alerta = min(reales) if reales else mejor["total"]
                if alerta:
                    # El umbral se confirma con el precio real de la aerolínea; y para no repetir
                    # la misma alerta, se compara contra la última con ese mismo precio.
                    motivo = motivo_umbral(precio_alerta) or motivo_bajada(mejor["total"], estado)
                    alerta = bool(motivo) and (ultima is None or precio_alerta <= ultima * (1 - REALERTA_SI_BAJA))
                    if not alerta:
                        print(f"Alerta descartada: precio real en aerolínea {cop(precio_alerta)}")
        browser.close()

    # Cada aviso lleva los cambios de estado que solo se guardan si de verdad sale,
    # así una alerta que no se pudo enviar se vuelve a intentar en la próxima revisión.
    avisos = []
    if not vuelos:
        motivo = "Google pidió verificación (captcha)" if bloqueado else "Google Flights no devolvió resultados"
        print(f"Sin resultados: {motivo}")
        # Un solo aviso de error por día; no se intenta saltar el captcha.
        if estado["ultimo_aviso_error"] != hoy:
            texto = f"{motivo}. El bot lo vuelve a intentar en la próxima revisión."
            avisos.append(("⚠️ Bot vuelos BOG→MCO: no se pudo revisar", texto, texto, {"ultimo_aviso_error": hoy}))
    else:
        mejor, cuerpo = detalle(vuelos, estado)
        if alerta:
            if confirmado:
                asunto = f"🔥 ¡Precio bueno! BOG→MCO directo {cop(precio_alerta)} COP (4 personas)"
            else:  # no se pudo leer el precio en la aerolínea: se avisa igual, pero sin prometer
                asunto = f"🔥 Posible precio bueno (sin confirmar): BOG→MCO desde {cop(precio_alerta)} COP"
            avisos.append((
                asunto,
                f"Por qué te aviso: {motivo}.\n\n{cuerpo}",
                texto_recordatorio(vuelos, motivo),
                {"ultima_alerta": precio_alerta},
            ))
        if resumen:
            intro = (
                "Resumen semanal: todavía no hay un precio que valga la pena, sigo vigilando.\n"
                f"Te aviso apenas baje de {cop(UMBRAL_POR_PERSONA)} por persona "
                f"o caiga {round(BAJADA_FUERTE * 100)}% bajo el mínimo visto.\n\n"
            )
            avisos.append((
                f"✈️ Resumen semanal BOG→MCO: desde {cop(mejor['total'])} COP",
                intro + cuerpo,
                intro + texto_recordatorio(vuelos),
                {"ultimo_resumen": hoy},
            ))
        estado["revisiones"].append({"fecha": ahora.isoformat(timespec="minutes"), "mejor": mejor["total"]})
        print(f"Mejor: {cop(mejor['total'])} · {mejor['aerolinea']} · ¿bueno? {motivo or 'no'}")

    fallo_aviso = False
    for asunto, texto, corto, cambios in avisos:
        print(f"\n{asunto}\n\n{corto if AVISAR_POR == 'recordatorios' else texto}")
        if dry_run:
            continue
        if avisar(asunto, texto, corto):
            estado.update(cambios)
            print(f"\nAviso enviado por {AVISAR_POR}")
            continue
        fallo_aviso = True
        if estado["ultimo_aviso_mail"] != hoy or asunto.startswith("🔥"):
            estado["ultimo_aviso_mail"] = hoy
            notificar_mac(
                "Bot de vuelos: el aviso no salió",
                f"Pendiente: {asunto}. Revisa {'Recordatorios' if AVISAR_POR == 'recordatorios' else 'Mail'}.",
            )
    if not dry_run:
        ESTADO.write_text(json.dumps(estado, indent=2))
    if fallo_aviso and EN_LA_NUBE:
        sys.exit(1)  # la corrida queda en rojo y GitHub te manda un correo


if __name__ == "__main__":
    main()
