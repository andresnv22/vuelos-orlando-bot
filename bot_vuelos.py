"""Bot de precios: vuelos directos Bogotá (BOG) -> Orlando (MCO), diciembre 2026.

Revisa en Google Flights varias combinaciones de fechas (mínimo 4 noches),
solo vuelos directos de Avianca y LATAM (sin Frontier), precios en COP para 4 adultos.
Solo manda correo cuando el precio real en la aerolínea (ida y vuelta en una sola reserva,
para todos) queda por debajo de UMBRAL_TOTAL, y lo repite en cada revisión mientras siga así.
Avisa una vez cuando el precio vuelve a subir, y manda un resumen diario (6pm) si ese día no hubo
otros avisos, para que sepas que sigue vivo aunque el precio esté sobre el tope.

Uso:
    python3 bot_vuelos.py              # revisa y avisa si hay algo bueno
    python3 bot_vuelos.py --dry-run    # revisa e imprime, sin correo ni guardar
    python3 bot_vuelos.py --resumen    # fuerza el correo de resumen

En GitHub Actions corre cada 3 horas (ver .github/workflows/bot.yml) y avisa por Gmail.
"""

import base64
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

from playwright.sync_api import sync_playwright

# ---------------- Configuración ----------------
# "recordatorios" (suena en el iPhone vía iCloud, solo desde el Mac), "gmail" (desde la nube) o "correo" (app Mail)
AVISAR_POR = os.environ.get("AVISAR_POR", "recordatorios")
EN_LA_NUBE = os.environ.get("GITHUB_ACTIONS") == "true"
CAPTURAS = os.environ.get("CAPTURAS")  # carpeta donde guardar lo que se veía cuando algo falla (GitHub la sube)
LISTA_RECORDATORIOS = "Vuelos Orlando"
EMAIL_TO = "andresxnz@outlook.com"

# Fechas, pasajeros y umbral viven en config.json (se cambian ahí, sin tocar el código).
CONFIG = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
PASAJEROS = CONFIG["pasajeros"]  # el joven de 17 paga tarifa de adulto
FECHAS_IDEALES = tuple(date.fromisoformat(f) for f in CONFIG["fechas_ideales"])
FLEXIBILIDAD_DIAS = CONFIG["flexibilidad_dias"]  # días antes o después, en la ida y en la vuelta
NOCHES_MIN, NOCHES_MAX = CONFIG["noches_min"], CONFIG["noches_max"]
# Solo se buscan estas aerolíneas (código IATA: nombre en Google). Sin Frontier: si entra en la búsqueda,
# Google arma el "desde" de LATAM o Avianca con la vuelta en Frontier, que son tiquetes separados.
AEROLINEAS = CONFIG["aerolineas"]
UMBRAL_TOTAL = CONFIG["umbral_total_cop"]  # COP ida y vuelta para todos: avisa si el precio queda por debajo
MAX_VERIFICAR = 8  # cuántas fechas como máximo se revisan en la página de la aerolínea por corrida
MARGEN_DESDE = 1.05  # el "desde" a veces sale un poco más alto que el real: también se revisan esas fechas
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
SEL_VUELO = '[aria-label*="round trip total"], [aria-label*="ida y vuelta"]'


def _campo(numero, valor):
    """Un campo del parámetro tfs de Google Flights (formato protobuf)."""
    def varint(n):
        out = b""
        while n > 0x7F:
            out += bytes([n & 0x7F | 0x80])
            n >>= 7
        return out + bytes([n])
    if isinstance(valor, int):
        return varint(numero << 3) + varint(valor)
    valor = valor.encode() if isinstance(valor, str) else valor
    return varint(numero << 3 | 2) + varint(len(valor)) + valor


def url_busqueda(salida, regreso):
    """Búsqueda de Google Flights de ida y vuelta, solo directos y solo AEROLINEAS, para PASAJEROS adultos.
    Es la misma que abre el link del correo, así ves exactamente lo que vio el bot."""
    def tramo(fecha, desde, hasta):
        return _campo(3, _campo(2, fecha.isoformat()) + _campo(5, 0)  # 5 = máximo de escalas
                      + b"".join(_campo(6, a) for a in AEROLINEAS)
                      + _campo(13, _campo(1, 1) + _campo(2, desde)) + _campo(14, _campo(1, 1) + _campo(2, hasta)))
    datos = (_campo(1, 28) + _campo(2, 2) + tramo(salida, "BOG", "MCO") + tramo(regreso, "MCO", "BOG")
             + _campo(8, 1) * PASAJEROS + _campo(9, 1) + _campo(19, 1))  # adultos, clase turista, ida y vuelta
    tfs = base64.urlsafe_b64encode(datos).decode().rstrip("=")
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&curr=COP&hl=en&gl=CO"


def en_espanol(url):
    return url.replace("hl=en", "hl=es")


def cop(n):
    return "$" + f"{n:,}".replace(",", ".")


MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def fecha_corta(iso):
    d = date.fromisoformat(iso)
    return f"{d.day} {MESES[d.month - 1]}"


OOPS = "Oops, something went wrong"  # error que Google muestra a veces en vez de los resultados


class ErrorDeGoogle(Exception):
    """Google respondió "Oops, something went wrong" en vez de los resultados."""


def hay_visible(page, locator):
    return locator.filter(visible=True).count() > 0


def cerrar_consentimiento(page):
    """Cierra avisos de cookies o consentimiento de Google si aparecen."""
    try:
        if not hasattr(page, "locator"):
            return
        btn = page.locator('button:has-text("Accept all"), button:has-text("Aceptar todo"), button:has-text("I agree"), button:has-text("Acepto todo")')
        if hay_visible(page, btn) and hasattr(btn, "first") and hasattr(btn.first, "click"):
            btn.first.click(timeout=2000)
            page.wait_for_timeout(1000)
    except Exception:
        pass


def intentar_recargar_oops(page):
    """Si Google muestra botón de 'Reload' o 'Volver a cargar', hace clic para reintentar la carga."""
    try:
        if not hasattr(page, "locator"):
            return False
        btn = page.locator("button:has-text('Reload'), button:has-text('Volver a cargar'), button:has-text('Reintentar')")
        if hay_visible(page, btn):
            if hasattr(btn, "first") and hasattr(btn.first, "click"):
                btn.first.click(timeout=3000)
                page.wait_for_timeout(2500)
                return True
            elif hasattr(btn, "click"):
                btn.click()
                page.wait_for_timeout(2500)
                return True
    except Exception:
        pass
    return False


def esperar_resultados(page, selector):
    """Espera hasta 30 s a que aparezca `selector`. Si Google responde "Oops, something went wrong",
    intenta recargar una vez con el botón de Reload si está presente. Si aún así persiste, lanza ErrorDeGoogle."""
    recargado = False
    for _ in range(60):
        if hay_visible(page, page.locator(selector)):
            return
        cerrar_consentimiento(page)
        if hay_visible(page, page.get_by_text(OOPS)):
            if not recargado and intentar_recargar_oops(page):
                recargado = True
                continue
            raise ErrorDeGoogle(OOPS)
        page.wait_for_timeout(500)
    raise TimeoutError(f"no aparecieron resultados ({selector})")


def buscar(page, salida, regreso):
    """Devuelve (vuelos, insight_google, bloqueado)."""
    for intento in range(3):  # Google a veces tarda, sobre todo al despertar el Mac
        try:
            page.goto(url_busqueda(salida, regreso), wait_until="domcontentloaded", timeout=60000)
            cerrar_consentimiento(page)
            break
        except Exception as e:
            print(f"  intento {intento + 1} falló: {type(e).__name__}")
            page.wait_for_timeout(5000 * (intento + 1))
    else:
        return [], None, False
    if "/sorry/" in page.url or "unusual traffic" in page.content().lower():
        return [], None, True
    try:
        esperar_resultados(page, SEL_VUELO)
    except Exception:
        try:
            # Reintento recargando si la carga inicial falló transitoriamente
            page.wait_for_timeout(2000)
            if hasattr(page, "reload"):
                page.reload(wait_until="domcontentloaded", timeout=30000)
                cerrar_consentimiento(page)
                esperar_resultados(page, SEL_VUELO)
            else:
                return [], None, False
        except Exception:
            return [], None, False
    page.wait_for_timeout(2500)  # dejar que termine de cargar la lista
    etiquetas = page.eval_on_selector_all(
        SEL_VUELO,
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
        if aerolinea not in AEROLINEAS.values():  # por si Google no aplica el filtro de aerolíneas
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
    """Lo que se muestra en el aviso (máx. 3): primero lo confirmado bajo el tope en la aerolínea,
    después tus fechas y luego lo más barato. `vuelos` viene ordenado por precio "desde"."""
    elegidos = sorted(
        (v for v in vuelos if v.get("precio_real") and motivo_umbral(v["precio_real"])),
        key=lambda v: v["precio_real"],
    )[:2]
    for v in [v for v in vuelos if es_ideal(v)][:1] + vuelos:
        if len(elegidos) == 3:
            break
        if v not in elegidos:
            elegidos.append(v)
    return elegidos


def verificar_precios(page, vuelos):
    """Revisa en la página de la aerolínea el vuelo más barato de cada fecha que en la búsqueda sale
    bajo el tope o cerca (tus fechas primero), hasta encontrar uno que de verdad lo esté. El precio "desde"
    de Google no predice bien el real, así que no basta con mirar solo las 2 o 3 más baratas."""
    por_fecha = {}
    for v in vuelos:
        por_fecha.setdefault((v["salida"], v["regreso"]), v)
    candidatos = sorted(por_fecha.values(), key=lambda v: (not es_ideal(v), v["total"]))
    for v in [v for v in candidatos if vale_revisar(v["total"])][:MAX_VERIFICAR]:
        completar_reserva(page, v)
        real = (cop(v["precio_real"]) if v.get("precio_real")
                else "solo tiquetes separados (no cuenta)" if v.get("solo_separados") else "no se pudo leer")
        print(f"  {v['aerolinea']} {v['salida']} → {v['regreso']}: desde {cop(v['total'])}, real {real}")
        if v.get("precio_real") and motivo_umbral(v["precio_real"]):
            return


def completar_reserva(page, v):
    """Pone en v el link de la página de reserva de Google (botón "Reservar con
    <aerolínea>" que lleva a su web oficial) y el precio real que da la aerolínea ahí.
    Cada intento va en una sesión nueva: en la de la búsqueda, esa misma búsqueda ya se abrió."""
    v.update(reserva=None, precio_real=None, solo_separados=False)
    for intento in range(3):  # Google a veces responde "Oops" aun en sesión nueva; al reintentar casi siempre sale
        if intento:
            page.wait_for_timeout(3000 * intento)
        pagina = sesion_nueva(page.context.browser)
        try:
            if v["reserva"]:
                # Ya se tiene el link y solo faltó el precio: se abre directo, sin repetir la búsqueda
                # (cada vez que se repite la misma búsqueda es más probable que Google responda "Oops").
                pagina.goto(v["reserva"].replace("hl=es", "hl=en"), wait_until="domcontentloaded", timeout=60000)
                v["precio_real"], v["solo_separados"] = leer_precio(pagina, v)
            elif reserva := link_reserva_google(pagina, v):
                v["reserva"] = reserva
                v["precio_real"], v["solo_separados"] = leer_precio(pagina, v)
        except Exception as e:
            print(f"  no se pudo abrir la página de reserva ({v['aerolinea']}): {type(e).__name__}")
        finally:
            pagina.context.close()
        if v["precio_real"] or v["solo_separados"]:
            return


def leer_precio(page, v):
    """(precio real, si solo había tiquetes separados) de la página de reserva ya abierta."""
    separados = False
    recargado = False
    for _ in range(12):  # los precios de la aerolínea tardan unos segundos en cargar
        cerrar_consentimiento(page)
        precio, separados = precio_ida_vuelta(page.inner_text("body"), v["aerolinea"])
        if precio:
            return precio, False
        if hay_visible(page, page.get_by_text(OOPS)):
            if not recargado and intentar_recargar_oops(page):
                recargado = True
                continue
            print(f'  la página de reserva respondió "{OOPS}"')
            break
        page.wait_for_timeout(2500)
    if not separados:
        capturar(page, f"{v['salida']}_{v['aerolinea']}_precio")
    return None, separados


def confirmar_precio(page, v):
    """Segunda lectura del precio real, en una sesión nueva y con el mismo link que va en el correo.
    Google a veces muestra por un rato un precio que después ya no está: el 11/11 a las 6:36pm el bot leyó
    $8.986.800 (Avianca 22 → 27 dic) y 15 minutos después, desde Colombia y desde GitHub, era $10.430.880.
    Devuelve el precio, o None si no se pudo leer."""
    for intento in range(2):
        if intento:
            page.wait_for_timeout(3000)
        pagina = sesion_nueva(page.context.browser)
        try:
            pagina.goto(v["reserva"].replace("hl=es", "hl=en"), wait_until="domcontentloaded", timeout=60000)
            precio, _ = leer_precio(pagina, v)
            if precio:
                if motivo_umbral(precio):  # queda la prueba de lo que mostraba la página (artefacto "capturas")
                    capturar(pagina, f"{v['salida']}_{v['aerolinea']}_aviso")
                return precio
        except Exception as e:
            print(f"  no se pudo volver a abrir la página de reserva: {type(e).__name__}")
        finally:
            pagina.context.close()
    return None


def confirmar_bajo_tope(page, vuelos):
    """Antes de avisar, vuelve a leer el precio de cada opción que salió bajo el tope y se queda con la
    segunda lectura (la más reciente). Si no se pudo leer otra vez, la opción queda sin confirmar y no se
    presenta como precio real."""
    for v in vuelos:
        if v.get("precio_real") and motivo_umbral(v["precio_real"]):
            primero = v["precio_real"]
            v["precio_real"] = confirmar_precio(page, v)
            v["sin_confirmar"] = v["precio_real"] is None
            despues = cop(v["precio_real"]) if v["precio_real"] else "no se pudo leer"
            print(f"  segunda lectura {v['aerolinea']} {v['salida']} → {v['regreso']}: {cop(primero)}, después {despues}")


def precio_ida_vuelta(texto, aerolinea):
    """Del texto de la página de reserva de Google, el precio de la reserva de ida y vuelta JUNTA
    con la aerolínea ("Book with Avianca · COP ..."). Las opciones de "Separate tickets" (dos
    tiquetes por aparte) y las de otras aerolíneas (p. ej. Delta, que vende vuelos de LATAM en dólares)
    no cuentan. Devuelve (precio o None, si solo había tiquetes separados)."""
    # Verificar que el precio aplique a todos los pasajeros (en inglés o español)
    valido_pasajeros = bool(
        re.search(
            rf"(?:for\s+{PASAJEROS}\s+(?:adults|passengers)|para\s+{PASAJEROS}\s+(?:adultos|pasajeros)|{PASAJEROS}\s+(?:passengers|pasajeros|adultos))",
            texto,
            re.I,
        )
    )
    if not valido_pasajeros:
        return None, False

    separados = False
    bloques = re.split(r"(?:Book with |Reservar con |Reservar en )", texto, flags=re.I)[1:]
    for opcion in bloques:
        bloque = opcion[:400]
        if any(sep in bloque for sep in ("Separate tickets", "Billetes separados", "Tiquetes separados")):
            separados = True
            continue
        if re.match(rf"{re.escape(aerolinea)}\s*(?:Airline|Airlines|Aerolínea|Compañía aérea)?", bloque, re.I):
            m = re.search(r"COP\s*([\d.,\xa0]+)", bloque)
            if m:
                limpio = re.sub(r"[.,\s\xa0]", "", m.group(1))
                if limpio.isdigit():
                    return int(limpio), False
    return None, separados


def link_reserva_google(page, v):
    """Elige ida y vuelta en Google Flights y devuelve el link de la página de reserva."""
    paso = "abrir la búsqueda"
    try:
        page.goto(v["url"], wait_until="domcontentloaded", timeout=60000)
        cerrar_consentimiento(page)
        esperar_resultados(page, SEL_VUELO)
        page.wait_for_timeout(2500)
        paso = "elegir la ida"
        page.locator(
            f'{SEL_VUELO}[aria-label*="Nonstop flight with {v["aerolinea"]}"][aria-label*="at {v["hora"]}"]'
        ).first.evaluate("e => e.click()", timeout=30000)  # el clic normal lo tapa otro elemento
        paso = "cargar las vueltas"
        sel_vueltas = (
            f'{SEL_VUELO}:is([aria-label*="Leaves Orlando"], [aria-label*="Sale de Orlando"], '
            f'[aria-label*="Orlando"], [aria-label*="MCO"])'
        )
        try:
            esperar_resultados(page, sel_vueltas)
        except Exception:
            esperar_resultados(page, SEL_VUELO)
        page.wait_for_timeout(2500)
        etiquetas = page.eval_on_selector_all(SEL_VUELO, "els => els.map(e => e.getAttribute('aria-label'))")
        regresos = [
            (int(m.group(1).replace(",", "")), i)
            for i, et in enumerate(etiquetas)
            if (m := ETIQUETA.search(et or "")) and m.group("aerolinea") == v["aerolinea"]
            and any(loc in (et or "") for loc in ("Leaves Orlando", "Sale de Orlando", "Orlando", "MCO"))
        ]
        if not regresos:
            regresos = [
                (int(m.group(1).replace(",", "")), i)
                for i, et in enumerate(etiquetas)
                if (m := ETIQUETA.search(et or "")) and m.group("aerolinea") == v["aerolinea"]
            ]
        if not regresos:
            return None
        paso = "abrir la página de reserva"
        page.locator(SEL_VUELO).nth(min(regresos)[1]).evaluate("e => e.click()")
        page.wait_for_url("**/travel/flights/booking**", timeout=30000)
        return en_espanol(page.url)
    except Exception as e:
        bloqueo = " (Google pidió verificación)" if "/sorry/" in page.url else ""
        print(f"  no se pudo sacar link de reserva ({v['aerolinea']}) al {paso}{bloqueo}: {type(e).__name__}")
        capturar(page, f"{v['salida']}_{v['aerolinea']}_{paso}")
        return None


def capturar(page, nombre):
    """Guarda en CAPTURAS una imagen y el texto de la página, para ver después qué mostraba Google."""
    if not CAPTURAS:
        return
    try:
        carpeta = Path(CAPTURAS)
        carpeta.mkdir(exist_ok=True, parents=True)
        base = carpeta / re.sub(r"[^\w-]+", "-", f"{datetime.now():%H%M%S}_{nombre}")
        if hasattr(page, "screenshot"):
            page.screenshot(path=f"{base}.png", full_page=True, timeout=15000)
        url = getattr(page, "url", "")
        title = page.title() if hasattr(page, "title") else ""
        text = page.inner_text('body', timeout=5000)[:8000] if hasattr(page, "inner_text") else ""
        Path(f"{base}.txt").write_text(f"{url}\n{title}\n\n{text}")
    except Exception as e:
        print(f"  no se pudo guardar la captura: {type(e).__name__}")


def motivo_umbral(total):
    """Motivo si el precio total (para los 4) queda debajo del tope, o None."""
    if total < UMBRAL_TOTAL:
        return f"{cop(total)} ida y vuelta para los {PASAJEROS}, menos de tu tope de {cop(UMBRAL_TOTAL)}"
    return None


def mejor_real(vuelos):
    """La opción con el precio real (de la aerolínea, ya confirmado) más bajo, o None."""
    return min((v for v in vuelos if v.get("precio_real")), key=lambda v: v["precio_real"], default=None)


def vale_revisar(desde):
    """Si con este precio "desde" vale la pena abrir la página de reserva para ver el real."""
    return desde < UMBRAL_TOTAL * MARGEN_DESDE


def detalle(vuelos, estado):
    vuelos = sorted(vuelos, key=lambda v: v["total"])
    mejor = vuelos[0]
    lineas = [
        f"Vuelos DIRECTOS Bogotá → Orlando · {PASAJEROS} adultos · ida y vuelta · solo {' y '.join(AEROLINEAS.values())}",
        f"Revisado: {datetime.now():%d/%m/%Y %H:%M}",
        "",
    ]
    real = mejor_real(vuelos)
    if real:
        lineas += [
            f"MEJOR PRECIO REAL: {cop(real['precio_real'])} COP en total ({cop(real['precio_real'] // PASAJEROS)} "
            f"por persona), ida y vuelta en una sola reserva con {real['aerolinea']}",
            f"  sale {fecha_corta(real['salida'])} {real['hora']} · regresa {fecha_corta(real['regreso'])} · "
            f"{real['noches']} noches",
            "",
        ]
    lineas += [
        f"Más barato en la búsqueda (\"desde\", no es el precio final): {cop(mejor['total'])} COP en total",
        f"  {mejor['aerolinea']} · sale {fecha_corta(mejor['salida'])} {mejor['hora']} · "
        f"regresa {fecha_corta(mejor['regreso'])} · {mejor['noches']} noches"
        + (" · tarifa básica, sin maleta de mano" if mejor["basica"] else ""),
    ]
    if mejor["insight"]:
        lineas.append(f"  Google dice que los precios para esas fechas están {INSIGHT_ES[mejor['insight']]}")
    if estado["revisiones"]:
        minimo = min(r["mejor"] for r in estado["revisiones"])
        lineas.append(f"  Mínimo \"desde\" visto desde que empezó el bot: {cop(min(minimo, mejor['total']))}")

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
                f"   Precio real ida y vuelta en una sola reserva con {v['aerolinea']}: {cop(v['precio_real'])} "
                f"({cop(v['precio_real'] // PASAJEROS)} por persona)"
            )
        if v.get("reserva"):
            lineas.append(f"   Ida y vuelta ya elegidas → botón \"Reservar con {v['aerolinea']}\" "
                          f"(no Delta ni \"Billetes separados\"): {v['reserva']}")
        lineas.append(f"   La búsqueda que hizo el bot (solo directos de {' y '.join(AEROLINEAS.values())}): "
                      f"{en_espanol(v['url'])}")
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
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "Mail" to return count of messages of outbox'],
            capture_output=True, text=True, timeout=10,
        )
        return int(r.stdout.strip() or 0)
    except Exception:
        return 0


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
    try:
        subprocess.run(["osascript", "-e", script, asunto, cuerpo, EMAIL_TO], check=True, timeout=30)
    except Exception as e:
        print(f"Error al enviar por Mail: {e}")
        return False
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
    try:
        r = subprocess.run(
            ["osascript", "-e", script, titulo, notas, LISTA_RECORDATORIOS], capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            print(f"No se pudo crear el recordatorio: {r.stderr.strip()}")
        return r.returncode == 0
    except Exception as e:
        print(f"No se pudo crear el recordatorio: {e}")
        return False


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
            f"   {cop(precio)} ida y vuelta para los {PASAJEROS} ({etiqueta}) · {cop(precio // PASAJEROS)} c/u",
            *([f"   Ida y vuelta en una sola reserva con {v['aerolinea']} (no son tiquetes separados)"]
              if v.get("precio_real") else []),
            f"   Reservar: {v.get('reserva') or en_espanol(v['url'])}",
            "",
        ]
    lineas += [
        f"Tu tope: menos de {cop(UMBRAL_TOTAL)} ida y vuelta para los {PASAJEROS}.",
        AYUDA_RESERVA,
        "Precios con impuestos; maletas facturadas aparte.",
    ]
    return "\n".join(lineas)


AYUDA_RESERVA = (
    "El link abre Google Flights con la ida y la vuelta ya elegidas. El precio de este correo es el de la línea "
    "\"Reservar con LATAM\" (o Avianca) · Compañía aérea: ida y vuelta en una sola compra en la web oficial. "
    "Si arriba sale un \"precio total más bajo\" distinto, es de otra aerolínea (Delta vende los mismos vuelos "
    "de LATAM y cobra en dólares) o de \"Billetes separados\" (dos compras por aparte): el bot no los cuenta. "
    f"Tampoco Frontier: el bot solo busca vuelos directos de {' y '.join(AEROLINEAS.values())}."
)


def grafica_png(estado):
    """Gráfica del mejor precio total en cada revisión, con la línea de tu tope. PNG o None."""
    try:
        puntos = []
        for r in estado.get("revisiones", []):
            try:
                if r.get("fecha", "").startswith("20") and r.get("mejor") is not None:
                    puntos.append((datetime.fromisoformat(r["fecha"]), float(r["mejor"]) / 1e6))
            except (ValueError, TypeError):
                continue
        if len(puntos) < 2:
            return None
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except (ImportError, Exception):
        return None

    try:
        fig, ax = plt.subplots(figsize=(7, 3), dpi=120)
        ax.plot([p[0] for p in puntos], [p[1] for p in puntos], color="#1a73e8", linewidth=2, marker="o", markersize=3,
                label=f'Precio "desde" en la búsqueda (no es el final)')
        reales = []
        for r in estado.get("revisiones", []):
            try:
                if r.get("real") and r.get("fecha", "").startswith("20"):
                    reales.append((datetime.fromisoformat(r["fecha"]), float(r["real"]) / 1e6))
            except (ValueError, TypeError):
                continue
        if reales:
            ax.scatter([p[0] for p in reales], [p[1] for p in reales], color="#e8710a", s=18, zorder=3,
                       label="Precio real en la aerolínea (cuando se revisó)")
        ax.axhline(UMBRAL_TOTAL / 1e6, color="#d93025", linestyle="--", linewidth=1.5, label="Tu tope")
        ax.set_ylabel("Millones COP ida y vuelta")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax.grid(alpha=0.3)
        ax.legend(loc="upper left", fontsize=8, frameon=False)
        for borde in ("top", "right"):
            ax.spines[borde].set_visible(False)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        print(f"No se pudo generar la gráfica: {type(e).__name__}: {e}")
        return None


def html_correo(intro, vuelos, con_grafica):
    """Versión HTML del correo: tabla con las mejores opciones y botones para reservar."""
    from html import escape
    opciones = sorted(
        opciones_para_reservar(sorted(vuelos, key=lambda v: v["total"])),
        key=lambda v: v.get("precio_real") or v["total"],
    )
    filas = ""
    for v in opciones:
        precio = v.get("precio_real") or v["total"]
        etiqueta = ("ida y vuelta en una sola reserva con " + escape(v["aerolinea"]) if v.get("precio_real")
                    else "desde (sin confirmar en la aerolínea)")
        estrella = '<div style="color:#b06000;font-weight:bold;font-size:12px">⭐ TUS FECHAS</div>' if es_ideal(v) else ""
        filas += f"""
        <div style="border:1px solid #e0e0e0;border-radius:10px;padding:14px;margin:0 0 12px">
          {estrella}<b style="font-size:15px">{escape(v['aerolinea'])}</b>
          <div style="color:#555;font-size:13px;margin:2px 0 8px">{fecha_corta(v['salida'])} → {fecha_corta(v['regreso'])} · {v['noches']} noches · sale {escape(v['hora'])}</div>
          <div style="font-size:20px;font-weight:bold">{cop(precio)} <span style="font-size:12px;font-weight:normal;color:#555">ida y vuelta para los {PASAJEROS}</span></div>
          <div style="color:#555;font-size:12px;margin-bottom:10px">{cop(precio // PASAJEROS)} por persona · {etiqueta}</div>
          <a href="{escape(v.get('reserva') or en_espanol(v['url']))}" style="display:inline-block;background:#1a73e8;color:#fff;text-decoration:none;padding:10px 18px;border-radius:6px;font-weight:bold">Reservar en {escape(v['aerolinea'])}</a>
          {f'<div style="color:#555;font-size:12px;margin-top:8px">En la página es la línea <b>"Reservar con {escape(v["aerolinea"])} · Compañía aérea"</b>, no la de otra aerolínea ni la de "Billetes separados".</div>' if v.get("precio_real") else ""}
          <div style="font-size:12px;margin-top:6px"><a href="{escape(en_espanol(v['url']))}" style="color:#1a73e8">Ver la búsqueda que hizo el bot</a> <span style="color:#555">(solo directos de {' y '.join(AEROLINEAS.values())})</span></div>
        </div>"""
    por_combo = {}
    for v in sorted(vuelos, key=lambda v: v["total"]):
        por_combo.setdefault((v["salida"], v["regreso"]), v)
    combos = "".join(
        f"<tr><td style='padding:4px 8px'>{'⭐ ' if es_ideal(v) else ''}{fecha_corta(s)} → {fecha_corta(r)} ({v['noches']} n)</td>"
        f"<td style='padding:4px 8px;text-align:right'>{cop(v['total'])}</td>"
        f"<td style='padding:4px 8px;color:#555'>{escape(v['aerolinea'])}</td></tr>"
        for (s, r), v in sorted(por_combo.items())
    )
    grafica = ('<h3 style="margin:24px 0 8px">Cómo ha ido el precio</h3><img src="cid:grafica" width="100%" '
               'style="max-width:600px" alt="Gráfica del precio">') if con_grafica else ""
    return f"""<!doctype html>
<html><body style="margin:0;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#202124">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:20px 16px">
  <h2 style="margin:0 0 4px">✈️ Bogotá → Orlando · vuelos directos</h2>
  <div style="color:#555;font-size:13px">{PASAJEROS} pasajeros · ida y vuelta · revisado {datetime.now():%d/%m/%Y %H:%M}</div>
  <p style="font-size:15px;line-height:1.5">{escape(intro).replace(chr(10), '<br>')}</p>
  {filas}
  <p style="font-size:12px;color:#555">{escape(AYUDA_RESERVA)}</p>
  {grafica}
  <h3 style="margin:24px 0 8px">Mejor precio "desde" por fechas</h3>
  <table style="border-collapse:collapse;font-size:13px">{combos}</table>
  <p style="font-size:12px;color:#777;margin-top:24px">Tu tope: menos de {cop(UMBRAL_TOTAL)} ida y vuelta para los {PASAJEROS}.
     Precios totales con impuestos; maletas facturadas aparte. Compra solo en la web oficial de la aerolínea.</p>
</div></body></html>"""


def enviar_gmail(asunto, cuerpo, html=None, png=None):
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
    msg.set_content(cuerpo)  # versión de texto, por si el lector de correo no muestra HTML
    if html:
        msg.add_alternative(html, subtype="html")
        if png:
            msg.get_payload()[1].add_related(png, "image", "png", cid="<grafica>")
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


def avisar(asunto, cuerpo_correo, texto_corto, html=None, png=None):
    if AVISAR_POR == "recordatorios":
        return crear_recordatorio(asunto, texto_corto)
    if AVISAR_POR == "gmail":
        texto = f"{texto_corto}\n\n{'-' * 60}\nDETALLE COMPLETO\n\n{cuerpo_correo}"
        return enviar_gmail(asunto, texto, html, png)
    return enviar_correo(asunto, cuerpo_correo)


def abrir_navegador(p):
    # En la nube se usa el Chromium que instala Playwright; en el Mac, tu Google Chrome.
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-infobars",
    ]
    canal = "chromium" if EN_LA_NUBE else "chrome"
    try:
        browser = p.chromium.launch(channel=canal, headless=True, args=args)
    except Exception:
        browser = p.chromium.launch(headless=True, args=args)
    return browser, sesion_nueva(browser)


def sesion_nueva(browser):
    """Una página en una sesión nueva del navegador con perfil de usuario real para evitar bloqueos."""
    page = browser.new_page(
        locale="en-US",
        timezone_id="America/Bogota",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9,es-CO;q=0.8,es;q=0.7",
        },
    )
    if hasattr(page, "add_init_script"):
        try:
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        except Exception:
            pass
    return page


def notificar_mac(titulo, mensaje):
    if EN_LA_NUBE:
        return
    try:
        subprocess.run([
            "osascript", "-e", "on run argv",
            "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
            "-e", "end run", titulo, mensaje,
        ], timeout=10)
    except Exception:
        pass


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
    intro = "Esto es una PRUEBA del bot (solo 2 fechas revisadas)."
    texto = f"{intro}\n\n" + texto_recordatorio(vuelos)
    print(texto)
    estado = json.loads(ESTADO.read_text()) if ESTADO.exists() else {"revisiones": []}
    png = grafica_png(estado)
    return avisar(f"🧪 Prueba bot vuelos BOG→MCO: desde {cop(vuelos[0]['total'])}", texto, texto,
                  html_correo(intro, vuelos, png is not None), png)


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
    estado.setdefault("bajo_tope", False)  # si el último aviso fue de un precio bajo el tope
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
                if not encontrados and not bloqueado:  # a veces no carga; se reintenta en una sesión nueva
                    page.context.close()
                    page = sesion_nueva(browser)
                    encontrados, _, bloqueado = buscar(page, salida, regreso)
            except Exception as e:  # una fecha que falla no tumba toda la revisión
                print(f"{salida} → {regreso}: error {type(e).__name__}")
                continue
            if bloqueado:
                break
            print(f"{salida} → {regreso}: {len(encontrados)} vuelos")
            vuelos += encontrados
            page.wait_for_timeout(random.randint(2000, 6000))  # ir despacio

        resumen = False
        motivo = precio_alerta = None
        sin_confirmar = []
        if vuelos:
            vuelos.sort(key=lambda v: v["total"])
            mejor = vuelos[0]
            resumen = forzar_resumen or (
                ahora.hour >= 18 and estado["ultimo_resumen"] != hoy  # uno al día, desde la revisión de las 6pm
            )
            # Filtro rápido con el precio "desde" (parecido al real): si ni siquiera queda cerca
            # del tope, no hace falta abrir las páginas de reserva.
            hay_candidatos = vale_revisar(mejor["total"])
            if hay_candidatos:
                verificar_precios(page, vuelos)
            if hay_candidatos or resumen:
                for v in opciones_para_reservar(vuelos):  # links de lo que se va a mostrar
                    if "reserva" not in v:
                        completar_reserva(page, v)
            # Solo cuenta el precio real de la aerolínea, ida y vuelta en una sola reserva. Cada opción
            # se juzga con su propio precio, y un "desde" sin confirmar nunca se presenta como bueno.
            revisados = [v for v in vuelos if "reserva" in v]
            confirmar_bajo_tope(page, revisados)  # un precio bajo el tope se lee dos veces antes de avisar
            bajo_tope = [v["precio_real"] for v in revisados if v.get("precio_real") and motivo_umbral(v["precio_real"])]
            if bajo_tope:
                precio_alerta = min(bajo_tope)
                motivo = motivo_umbral(precio_alerta)
            else:
                sin_confirmar = [v["total"] for v in revisados if not v.get("precio_real")
                                 and not v.get("solo_separados") and (motivo_umbral(v["total"]) or v.get("sin_confirmar"))]
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
            avisos.append(("⚠️ Bot vuelos BOG→MCO: no se pudo revisar", texto, texto, {"ultimo_aviso_error": hoy}, None))
    else:
        mejor, cuerpo = detalle(vuelos, estado)
        # La hora en el asunto hace que cada aviso llegue como correo nuevo y no escondido en el anterior.
        cuando = f"{fecha_corta(hoy)}, {ahora.hour % 12 or 12}:{ahora:%M} {'am' if ahora.hour < 12 else 'pm'}"
        if precio_alerta:
            # Se avisa en cada revisión (cada 3 horas) mientras el precio real siga bajo el tope.
            avisos.append((
                f"🔥 ¡Precio bueno! BOG→MCO directo {cop(precio_alerta)} COP (4 personas) · {cuando}",
                f"Por qué te aviso: {motivo}.\n\n{cuerpo}",
                texto_recordatorio(vuelos, motivo),
                {"bajo_tope": True},
                f"🔥 Por qué te aviso: {motivo}.",
            ))
        elif sin_confirmar:
            # La página de la aerolínea no mostró el precio: no se sabe si está bajo el tope, y el
            # "desde" no sirve para decidir (suele salir bastante más bajo que el real).
            print(f"No se pudo confirmar el precio real (desde {cop(min(sin_confirmar))})")
            if estado["ultimo_aviso_error"] != hoy:  # un solo aviso de este tipo por día
                intro = (
                    "No pude leer (o confirmar con una segunda lectura) el precio real en la página de la aerolínea, "
                    f"así que no sé si hay algo bajo tu tope de {cop(UMBRAL_TOTAL)}. En la búsqueda sale desde "
                    f"{cop(min(sin_confirmar))}, que no es el precio final. Lo vuelvo a intentar en la próxima revisión."
                )
                avisos.append((
                    "⚠️ Bot vuelos BOG→MCO: no pude confirmar el precio real",
                    f"{intro}\n\n{cuerpo}",
                    f"{intro}\n\n{texto_recordatorio(vuelos)}",
                    {"ultimo_aviso_error": hoy},
                    f"⚠️ {intro}",
                ))
        elif estado["bajo_tope"]:
            # Venía avisando cada 3 horas y ya no hay nada bajo el tope: se avisa una vez, para que el
            # silencio que sigue no parezca que el bot dejó de funcionar.
            intro = (
                f"El precio subió: en las fechas que revisé ya no hay nada bajo tu tope de {cop(UMBRAL_TOTAL)} "
                f"ida y vuelta para los {PASAJEROS}. Sigo revisando cada 3 horas y te aviso apenas vuelva a bajar."
            )
            avisos.append((
                f"⬆️ Subió el precio BOG→MCO: ya no está bajo tu tope · {cuando}",
                f"{intro}\n\n{cuerpo}",
                f"{intro}\n\n{texto_recordatorio(vuelos)}",
                {"bajo_tope": False},
                f"⬆️ {intro}",
            ))
        # Si ya sale otro aviso, ese mismo dice que el bot sigue vivo: el resumen ("todavía no hay un
        # precio que valga la pena") sobra y se manda en otra revisión sin avisos.
        if resumen and (forzar_resumen or not avisos):
            intro = (
                "Resumen del día: el bot sigue revisando cada 3 horas, pero todavía no hay un precio real "
                f"bajo tu tope.\n"
                f"Te aviso apenas el precio real baje de {cop(UMBRAL_TOTAL)} ida y vuelta para los {PASAJEROS}.\n\n"
            )
            real = mejor_real(vuelos)
            avisos.append((
                f"✈️ Resumen del día BOG→MCO: precio real más bajo {cop(real['precio_real'])} COP" if real
                else f"✈️ Resumen del día BOG→MCO: lo más barato desde {cop(mejor['total'])} COP (sin confirmar)",
                intro + cuerpo,
                intro + texto_recordatorio(vuelos),
                {"ultimo_resumen": hoy},
                intro.strip(),
            ))
        revision = {"fecha": ahora.isoformat(timespec="minutes"), "mejor": mejor["total"]}
        if mejor_real(vuelos):  # el precio real solo se conoce cuando se abrió la página de reserva
            revision["real"] = mejor_real(vuelos)["precio_real"]
        estado["revisiones"].append(revision)
        print(f"Mejor: {cop(mejor['total'])} · {mejor['aerolinea']} · ¿bueno? {motivo or 'no'}")

    fallo_aviso = False
    png = grafica_png(estado) if avisos and AVISAR_POR == "gmail" else None
    for asunto, texto, corto, cambios, intro_html in avisos:
        print(f"\n{asunto}\n\n{corto if AVISAR_POR == 'recordatorios' else texto}")
        if dry_run:
            continue
        html = html_correo(intro_html, vuelos, png is not None) if intro_html and vuelos else None
        if avisar(asunto, texto, corto, html, png if html else None):
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
