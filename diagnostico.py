"""Diagnóstico: abre la página de reserva del vuelo más barato de cada fecha, mide cuánto tarda y
vuelve a leer el precio con el link del correo (la segunda lectura que el bot hace antes de avisar).

No manda correos ni guarda historial. Cuando un link no sale, bot_vuelos guarda una captura en
CAPTURAS (en GitHub: Actions → Run workflow → modo diagnostico, y el artefacto "capturas").
"""
import time
from datetime import date

from playwright.sync_api import sync_playwright

import bot_vuelos as b

with sync_playwright() as p:
    browser, page = b.abrir_navegador(p)
    resultados, iguales = [], []
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
        segunda = b.confirmar_precio(page, v) if v["precio_real"] else None
        vuelta_txt = f", vuelta {v['hora_regreso']}" if v.get("hora_regreso") else ""
        print(f"{salida} → {regreso} {v['aerolinea']} {v['hora']}{vuelta_txt}: desde {b.cop(v['total'])}, "
              f"real {real}, segunda lectura {b.cop(segunda) if segunda else 'sin precio'}, {segundos:.0f} s")
        resultados.append(bool(v["precio_real"]))
        iguales.append(bool(segunda) and segunda == v["precio_real"])
    browser.close()
print(f"\nPáginas de reserva con precio: {sum(resultados)} de {len(resultados)}")
print(f"Segunda lectura igual a la primera: {sum(iguales)} de {len(iguales)}")
