"""Diagnóstico: abre la página de reserva del vuelo más barato de cada fecha y mide cuánto tarda.

No manda correos ni guarda historial. Cuando un link no sale, bot_vuelos guarda una captura en
CAPTURAS (en GitHub: Actions → Run workflow → modo diagnostico, y el artefacto "capturas").
"""
import time
from datetime import date

from playwright.sync_api import sync_playwright

import bot_vuelos as b

with sync_playwright() as p:
    browser, page = b.abrir_navegador(p)
    resultados = []
    for salida, regreso in b.COMBINACIONES:
        if salida <= date.today():
            continue
        vuelos, _, bloqueado = b.buscar(page, salida, regreso)
        if not vuelos and not bloqueado:  # igual que el bot: se reintenta en una sesión nueva
            page.context.close()
            page = b.sesion_nueva(browser)
            vuelos, _, bloqueado = b.buscar(page, salida, regreso)
        if bloqueado:
            print("Google pidió verificación (captcha) en la búsqueda; se para aquí.")
            break
        if not vuelos:
            print(f"{salida} → {regreso}: la búsqueda no trajo vuelos")
            continue
        v = min(vuelos, key=lambda v: v["total"])
        inicio = time.time()
        b.completar_reserva(page, v)
        segundos = time.time() - inicio
        real = b.cop(v["precio_real"]) if v["precio_real"] else "sin precio"
        print(f"{salida} → {regreso} {v['aerolinea']} {v['hora']}: desde {b.cop(v['total'])}, "
              f"real {real}, {segundos:.0f} s")
        resultados.append(bool(v["precio_real"]))
    browser.close()
print(f"\nPáginas de reserva con precio: {sum(resultados)} de {len(resultados)}")
