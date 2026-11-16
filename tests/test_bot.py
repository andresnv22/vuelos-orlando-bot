"""Pruebas del bot con precios simulados: no abren Google ni mandan correos.

Correr con:  python -m unittest discover -s tests -v
"""
import base64
import contextlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot_vuelos as b  # noqa: E402


class _Falso:
    """Hace de navegador, de sesión y de página: el bot les llama close(), wait_for_timeout() y new_page()."""

    @property
    def context(self):
        return self

    @property
    def browser(self):
        return self

    def new_page(self, **k):
        return type(self)()

    def close(self):
        pass

    def wait_for_timeout(self, ms):
        pass


@contextlib.contextmanager
def _sin_playwright():
    yield None


class PruebaAlertas(unittest.TestCase):
    def setUp(self):
        self.precios = {}
        self.enviados = []
        self.originales = {n: getattr(b, n) for n in
                           ("sync_playwright", "abrir_navegador", "buscar", "completar_reserva", "avisar", "ESTADO",
                            "datetime", "confirmar_precio")}
        # Reloj fijo para que el resultado no dependa del día en que se corren las pruebas:
        # a las 10am no toca el resumen del día (sale desde la revisión de las 6pm).
        self.ahora = datetime(2026, 11, 3, 10, 0)
        prueba = self

        class Reloj(datetime):
            @classmethod
            def now(cls, tz=None):
                return prueba.ahora

        b.datetime = Reloj
        b.sync_playwright = _sin_playwright
        b.abrir_navegador = lambda p: (_Falso(), _Falso())
        b.buscar = self._buscar
        b.completar_reserva = self._completar
        # Segunda lectura antes de avisar: por defecto sale igual que la primera.
        self.segunda = "igual"
        b.confirmar_precio = lambda page, v: v["precio_real"] if self.segunda == "igual" else self.segunda
        b.avisar = lambda asunto, *resto: self.enviados.append(asunto) or True
        self.tmp = tempfile.TemporaryDirectory()
        b.ESTADO = Path(self.tmp.name) / "estado.json"
        self.guardar_estado([])
        self.argv = sys.argv
        sys.argv = ["bot"]

    def tearDown(self):
        for nombre, valor in self.originales.items():
            setattr(b, nombre, valor)
        sys.argv = self.argv
        self.tmp.cleanup()

    def guardar_estado(self, revisiones):
        b.ESTADO.write_text(json.dumps({"revisiones": revisiones}))

    def _buscar(self, page, salida, regreso):
        if (salida, regreso) != b.FECHAS_IDEALES:
            return [], None, False
        return [{
            "salida": salida.isoformat(), "regreso": regreso.isoformat(), "noches": (regreso - salida).days,
            "aerolinea": "Avianca", "hora": "4:20 PM", "total": self.precios["desde"],
            "basica": False, "insight": None, "url": "https://ejemplo",
        }], None, False

    def _completar(self, page, v):
        v["reserva"] = "https://reserva"
        v["precio_real"] = self.precios["real"]

    def revisar(self, desde, real):
        """Corre una revisión completa y devuelve los asuntos de los avisos que mandó."""
        self.precios.update(desde=desde, real=real)
        antes = len(self.enviados)
        b.main()
        return self.enviados[antes:]

    def umbral_total(self, factor):
        return int(b.UMBRAL_TOTAL * factor)

    def test_avisa_si_el_precio_real_queda_bajo_el_tope(self):
        avisos = self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))
        self.assertEqual(len(avisos), 1)
        self.assertIn("¡Precio bueno!", avisos[0])

    def test_repite_en_cada_revision_mientras_siga_bajo_el_tope(self):
        for _ in range(3):
            self.assertEqual(len(self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))), 1)

    def test_avisa_una_sola_vez_cuando_el_precio_sube(self):
        self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))
        subio = self.revisar(self.umbral_total(0.94), self.umbral_total(1.02))  # el real ya no está bajo el tope
        self.assertEqual(len(subio), 1)
        self.assertIn("Subió el precio", subio[0])
        self.assertEqual(self.revisar(self.umbral_total(1.3), self.umbral_total(1.4)), [])
        self.assertEqual(len(self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))), 1)  # bajó otra vez

    def test_revisa_la_aerolinea_aunque_el_desde_salga_un_poco_sobre_el_tope(self):
        avisos = self.revisar(self.umbral_total(1.03), self.umbral_total(0.98))
        self.assertEqual(len(avisos), 1)
        self.assertIn("¡Precio bueno!", avisos[0])

    def test_sin_subir_desde_un_precio_bueno_no_avisa_que_subio(self):
        self.assertEqual(self.revisar(self.umbral_total(1.3), self.umbral_total(1.4)), [])

    def test_no_avisa_si_solo_el_desde_es_bueno(self):
        self.assertEqual(self.revisar(self.umbral_total(0.95), self.umbral_total(1.07)), [])

    def test_sin_precio_real_avisa_una_vez_al_dia_que_no_pudo_confirmar(self):
        avisos = self.revisar(self.umbral_total(0.85), None)
        self.assertEqual(len(avisos), 1)
        self.assertIn("no pude confirmar", avisos[0])
        self.assertNotIn("Precio bueno", avisos[0])
        self.assertEqual(self.revisar(self.umbral_total(0.85), None), [])

    def test_nunca_dice_precio_bueno_sobre_el_tope_aunque_el_desde_baje_mucho(self):
        historial = self.umbral_total(1.20)
        self.guardar_estado([{"fecha": "2026-11-01T06:00", "mejor": historial}] * 10)
        self.assertEqual(self.revisar(self.umbral_total(0.90), self.umbral_total(1.05)), [])

    def test_no_descarta_por_el_precio_real_de_otra_opcion(self):
        self.precios.update(desde=0, real=None)
        ideal = self._buscar(None, *b.FECHAS_IDEALES)[0][0]
        barato = {**ideal, "aerolinea": "Avianca", "total": self.umbral_total(0.86)}
        caro = {**ideal, "aerolinea": "LATAM", "hora": "9:00 AM", "total": self.umbral_total(1.01)}
        b.buscar = lambda page, s, r: ([barato, caro], None, False) if (s, r) == b.FECHAS_IDEALES else ([], None, False)

        def completar(page, v):  # el barato no muestra su precio real; el caro sí, y está sobre el tope
            v["reserva"] = "https://reserva"
            v["precio_real"] = None if v["aerolinea"] == "Avianca" else self.umbral_total(1.24)

        b.completar_reserva = completar
        avisos = self.revisar(0, 0)
        self.assertEqual(len(avisos), 1)
        self.assertIn("no pude confirmar", avisos[0])  # no se descarta en silencio

    def test_revisa_otras_fechas_si_la_mas_barata_sale_cara_en_la_aerolinea(self):
        otras = next(c for c in b.COMBINACIONES if c != b.FECHAS_IDEALES)

        def vuelo(fechas, aerolinea, total):
            s, r = fechas
            return {"salida": s.isoformat(), "regreso": r.isoformat(), "noches": (r - s).days,
                    "aerolinea": aerolinea, "hora": "4:20 PM", "total": total,
                    "basica": False, "insight": None, "url": "https://ejemplo"}

        # Tus fechas: barato en la búsqueda pero caro en la aerolínea. Otras fechas: un poco más
        # caro en la búsqueda pero de verdad bajo el tope en la aerolínea.
        resultados = {b.FECHAS_IDEALES: vuelo(b.FECHAS_IDEALES, "LATAM", self.umbral_total(0.86)),
                      otras: vuelo(otras, "Avianca", self.umbral_total(0.88))}
        reales = {"LATAM": self.umbral_total(1.33), "Avianca": self.umbral_total(0.95)}
        b.buscar = lambda page, s, r: ([dict(resultados[(s, r)])], None, False) if (s, r) in resultados else ([], None, False)

        def completar(page, v):
            v["reserva"] = "https://reserva"
            v["precio_real"] = reales[v["aerolinea"]]

        b.completar_reserva = completar
        avisos = self.revisar(0, 0)
        self.assertEqual(len(avisos), 1)
        self.assertIn("¡Precio bueno!", avisos[0])
        self.assertIn(b.cop(reales["Avianca"]), avisos[0])

    def test_resumen_del_dia_solo_si_ese_dia_no_salio_otro_aviso(self):
        self.ahora = datetime(2026, 11, 3, 18, 0)
        con_alerta = self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))
        subio = self.revisar(self.umbral_total(1.3), self.umbral_total(1.4))
        sin_avisos = self.revisar(self.umbral_total(1.3), self.umbral_total(1.4))
        self.assertEqual(len(con_alerta), 1)
        self.assertIn("¡Precio bueno!", con_alerta[0])
        self.assertEqual(len(subio), 1)
        self.assertIn("Subió el precio", subio[0])
        self.assertEqual(len(sin_avisos), 1)
        self.assertIn("Resumen del día", sin_avisos[0])
        self.assertEqual(self.revisar(self.umbral_total(1.3), self.umbral_total(1.4)), [])  # uno solo al día

    def test_reintenta_una_fecha_que_no_cargo(self):
        llamadas = []

        def buscar(page, s, r):  # la primera vez tus fechas no cargan
            llamadas.append((s, r))
            if (s, r) == b.FECHAS_IDEALES and llamadas.count((s, r)) == 1:
                return [], None, False
            return self._buscar(page, s, r)

        b.buscar = buscar
        avisos = self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))
        self.assertEqual(len(avisos), 1)
        self.assertIn("¡Precio bueno!", avisos[0])

    def test_no_avisa_si_la_segunda_lectura_ya_no_esta_bajo_el_tope(self):
        # Como el 11/11 a las 6:36pm: la página mostró un rato un precio que después ya no estaba.
        self.segunda = self.umbral_total(1.04)
        avisos = self.revisar(self.umbral_total(1.04), self.umbral_total(0.90))
        self.assertFalse(any("Precio bueno" in a for a in avisos))

    def test_avisa_con_el_precio_de_la_segunda_lectura(self):
        self.segunda = self.umbral_total(0.97)
        avisos = self.revisar(self.umbral_total(0.94), self.umbral_total(0.90))
        self.assertEqual(len(avisos), 1)
        self.assertIn(b.cop(self.umbral_total(0.97)), avisos[0])

    def test_si_la_segunda_lectura_no_carga_no_dice_precio_bueno(self):
        self.segunda = None
        avisos = self.revisar(self.umbral_total(1.04), self.umbral_total(0.90))
        self.assertEqual(len(avisos), 1)
        self.assertIn("no pude confirmar", avisos[0])

    def test_el_resumen_del_dia_dice_el_precio_real(self):
        self.ahora = datetime(2026, 11, 3, 18, 0)
        avisos = self.revisar(self.umbral_total(1.3), self.umbral_total(1.12))
        self.assertEqual(len(avisos), 1)
        self.assertIn(f"precio real más bajo {b.cop(self.umbral_total(1.12))}", avisos[0])

    def test_guarda_el_historial(self):
        self.revisar(self.umbral_total(1.3), self.umbral_total(1.4))
        self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))
        revisiones = json.loads(b.ESTADO.read_text())["revisiones"]
        self.assertEqual(len(revisiones), 2)
        self.assertNotIn("real", revisiones[0])  # el "desde" quedó lejos del tope: no se abrió la reserva
        self.assertEqual((revisiones[1]["mejor"], revisiones[1]["real"]),
                         (self.umbral_total(0.94), self.umbral_total(0.98)))


class PruebaCorreoYConfig(unittest.TestCase):
    def vuelo(self, salida, regreso, total, real=None):
        return {"salida": salida, "regreso": regreso, "noches": 5, "aerolinea": "LATAM", "hora": "9:00 AM",
                "total": total, "precio_real": real, "reserva": "https://reserva", "url": "https://x"}

    def test_las_fechas_ideales_van_primero(self):
        self.assertEqual(b.COMBINACIONES[0], b.FECHAS_IDEALES)
        for salida, regreso in b.COMBINACIONES:
            self.assertTrue(b.NOCHES_MIN <= (regreso - salida).days <= b.NOCHES_MAX)

    def test_correo_html_tiene_botones_y_marca_tus_fechas(self):
        ideal = [d.isoformat() for d in b.FECHAS_IDEALES]
        html = b.html_correo("intro", [self.vuelo(*ideal, 9_000_000, 9_500_000)], False)
        self.assertIn("Reservar en LATAM", html)
        self.assertIn("TUS FECHAS", html)
        self.assertIn(b.cop(9_500_000), html)

    def test_grafica_es_un_png(self):
        estado = {"revisiones": [{"fecha": f"2026-11-0{d}T06:00", "mejor": 9_000_000 + d} for d in range(1, 4)]}
        estado["revisiones"][1]["real"] = 9_400_000
        png = b.grafica_png(estado)
        if png is None:
            self.skipTest("matplotlib no está instalado")
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_grafica_ignora_registros_corruptos(self):
        estado = {
            "revisiones": [
                {"fecha": "corrupta", "mejor": "no_es_numero"},
                {"fecha": "2026-11-01T06:00", "mejor": 9_000_000},
                {"fecha": "2026-11-02T06:00", "mejor": 9_200_000, "real": "invalido"},
            ]
        }
        png = b.grafica_png(estado)
        if png is None:
            self.skipTest("matplotlib no está instalado")
        self.assertTrue(png.startswith(b"\x89PNG"))



class PruebaBusqueda(unittest.TestCase):
    def test_busca_solo_directos_de_avianca_y_latam_para_los_4(self):
        tfs = b.url_busqueda(*b.FECHAS_IDEALES).split("tfs=")[1].split("&")[0]
        datos = base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4))
        self.assertEqual(datos.count(b"\x28\x00"), 2)  # máximo 0 escalas, en la ida y en la vuelta
        self.assertEqual(datos.count(b"\x32\x02AV"), 2)
        self.assertEqual(datos.count(b"\x32\x02LA"), 2)
        self.assertNotIn(b"F9", datos)  # Frontier
        self.assertEqual(datos.count(b"\x40\x01"), b.PASAJEROS)  # adultos
        self.assertIn(b"2026-12-22", datos)
        self.assertIn(b"2026-12-27", datos)

    def test_descarta_otras_aerolineas_si_google_no_aplica_el_filtro(self):
        etiquetas = [
            "From 4536736 Colombian pesos round trip total.This price does not include overhead bin access. "
            "Nonstop flight with Frontier. Leaves El Dorado International Airport at 3:20 PM on Tuesday",
            "From 9517440 Colombian pesos round trip total. Nonstop flight with LATAM. Operated by Latam Airlines "
            "Colombia. Leaves El Dorado International Airport at 10:35 AM on Wednesday",
        ]

        class Pagina(_Falso):
            url = "https://www.google.com/travel/flights/search"

            def goto(self, *a, **k):
                pass

            def content(self):
                return ""

            def locator(self, selector):
                return _Visibles(True)

            def get_by_text(self, texto):
                return _Visibles(False)

            def eval_on_selector_all(self, *a):
                return etiquetas

            def inner_text(self, selector):
                return ""

        vuelos, _, _ = b.buscar(Pagina(), *b.FECHAS_IDEALES)
        self.assertEqual([(v["aerolinea"], v["total"]) for v in vuelos], [("LATAM", 9_517_440)])


class _Visibles:
    """Hace de locator: solo dice si hay algo visible."""

    def __init__(self, hay):
        self.hay = hay

    def filter(self, visible):
        return self

    def count(self):
        return 1 if self.hay else 0


class PaginaGoogle(_Falso):
    """Página de resultados que carga bien o que muestra "Oops, something went wrong"."""

    def __init__(self, oops=False):
        self.oops = oops

    def locator(self, selector):
        return _Visibles(not self.oops)

    def get_by_text(self, texto):
        return _Visibles(self.oops)


class PruebaErrorDeGoogle(unittest.TestCase):
    def test_espera_los_resultados(self):
        b.esperar_resultados(PaginaGoogle(), b.SEL_VUELO)

    def test_si_google_responde_oops_falla_de_una_vez_para_reintentar_en_sesion_nueva(self):
        with self.assertRaises(b.ErrorDeGoogle):
            b.esperar_resultados(PaginaGoogle(oops=True), b.SEL_VUELO)

    def test_cada_intento_de_reserva_va_en_una_sesion_nueva(self):
        usadas = []
        original = b.link_reserva_google
        b.link_reserva_google = lambda page, v: usadas.append(page) or ("https://reserva" if len(usadas) > 1 else None)

        class Pagina(_Falso):
            cerrada = False

            def close(self):
                self.cerrada = True

            def inner_text(self, selector):
                return PruebaPaginaDeReserva.JUNTA

        principal = Pagina()
        try:
            v = {"aerolinea": "Avianca"}
            b.completar_reserva(principal, v)
        finally:
            b.link_reserva_google = original
        self.assertEqual(len(usadas), 2)  # el primer intento falló y el segundo no
        self.assertNotIn(principal, usadas)  # nunca en la sesión de la búsqueda
        self.assertIsNot(usadas[0], usadas[1])
        self.assertTrue(all(pagina.cerrada for pagina in usadas))
        self.assertEqual((v["reserva"], v["precio_real"]), ("https://reserva", 9_556_240))

    def test_si_ya_tiene_el_link_reintenta_abriendolo_directo_sin_repetir_la_busqueda(self):
        busquedas, abiertas = [], []
        original = b.link_reserva_google
        b.link_reserva_google = lambda page, v: busquedas.append(page) or "https://reserva?hl=es"

        class Pagina(_Falso):
            def goto(self, url, **k):
                abiertas.append(url)

            def get_by_text(self, texto):  # la primera página de reserva respondió "Oops"
                return _Visibles(not abiertas)

            def inner_text(self, selector):
                return PruebaPaginaDeReserva.JUNTA if abiertas else "Oops, something went wrong"

        try:
            v = {"aerolinea": "Avianca", "salida": "2026-12-22"}
            b.completar_reserva(Pagina(), v)
        finally:
            b.link_reserva_google = original
        self.assertEqual(len(busquedas), 1)
        self.assertEqual(abiertas, ["https://reserva?hl=en"])
        self.assertEqual(v["precio_real"], 9_556_240)


class PruebaPaginaDeReserva(unittest.TestCase):
    # Textos como los que muestra Google Flights en la página de reserva (en inglés).
    PIE = "Prices include required taxes + fees for 4 adults. Optional charges and bag fees may apply.\n"
    JUNTA = "Booking options\nBook with AviancaAirline\nCOP\xa09,556,240\nContinue\nView options\n" + PIE
    # Copiado de la página real (LATAM 23 → 28 dic, 2/11/2026): Delta vende los mismos vuelos más barato
    # y cobra en dólares, y hay una opción de billetes separados.
    CON_DELTA = ("4 passengers\nCOP 9,256,091\nLowest total price\nBooking options\n"
                 "Book with DeltaAirline\nCOP 9,256,091\n$2,898\nContinue\nView options\n"
                 "Book with LATAMAirline\nCOP 9,461,400\nContinue\nView options\n"
                 "Book with LATAMAirline\nSeparate ticketsThe 2 tickets in your itinerary must be booked "
                 "individually\nCOP 11,670,840\nContinue\nView options\n") + PIE
    SEPARADOS = ("Book with LATAM, DeltaAirline\nSeparate tickets\nThe 2 tickets in this itinerary must be "
                 "booked separately\nCOP\xa014,748,866\nContinue\n")
    SEPARADOS_MISMA = "Book with LATAMAirline\nSeparate tickets\nCOP\xa08,100,000\nContinue\n"

    def test_lee_la_reserva_de_ida_y_vuelta_junta(self):
        self.assertEqual(b.precio_ida_vuelta(self.JUNTA, "Avianca"), (9_556_240, False))

    def test_ignora_tiquetes_separados_aunque_sean_mas_baratos(self):
        texto = ("Booking options\n" + self.SEPARADOS_MISMA + "Book with LATAMAirline\nCOP\xa011,288,920\nContinue\n"
                 + self.PIE)
        self.assertEqual(b.precio_ida_vuelta(texto, "LATAM"), (11_288_920, False))

    def test_si_solo_hay_separados_no_da_precio(self):
        self.assertEqual(b.precio_ida_vuelta("Booking options\n" + self.SEPARADOS_MISMA + self.PIE, "LATAM"),
                         (None, True))

    def test_no_cuenta_el_precio_de_delta_aunque_sea_el_mas_bajo(self):
        self.assertEqual(b.precio_ida_vuelta(self.CON_DELTA, "LATAM"), (9_461_400, False))

    def test_no_lee_un_precio_que_no_sea_para_los_4(self):
        texto = self.JUNTA.replace("for 4 adults", "for 1 adult")
        self.assertEqual(b.precio_ida_vuelta(texto, "Avianca"), (None, False))

    def test_no_confunde_otra_aerolinea(self):
        self.assertEqual(b.precio_ida_vuelta(self.JUNTA, "LATAM"), (None, False))

    def test_lee_reserva_en_espanol(self):
        texto = (
            "Opciones para reservar\n"
            "Reservar con Avianca · Compañía aérea\n"
            "COP 9.462.200\n"
            "Continuar\n"
            "Los precios incluyen tasas e impuestos obligatorios para 4 adultos."
        )
        self.assertEqual(b.precio_ida_vuelta(texto, "Avianca"), (9_462_200, False))

    def test_lee_reserva_con_4_pasajeros_en_espanol(self):
        texto = (
            "4 pasajeros\n"
            "COP 9.256.091\n"
            "Reservar con LATAM · Compañía aérea\n"
            "COP 9.461.400\n"
            "Continuar"
        )
        self.assertEqual(b.precio_ida_vuelta(texto, "LATAM"), (9_461_400, False))

    def test_detecta_tiquetes_separados_en_espanol(self):
        texto = (
            "Opciones para reservar\n"
            "Reservar con Avianca\n"
            "Billetes separados\n"
            "COP 8.100.000\n"
            "para 4 adultos"
        )
        self.assertEqual(b.precio_ida_vuelta(texto, "Avianca"), (None, True))


class PruebaSegundaLectura(unittest.TestCase):
    def test_vuelve_a_abrir_el_link_del_correo_en_una_sesion_nueva(self):
        abiertas = []

        class Pagina(_Falso):
            def goto(self, url, **k):
                abiertas.append((self, url))

            def inner_text(self, selector):
                return PruebaPaginaDeReserva.JUNTA

        principal = Pagina()
        v = {"aerolinea": "Avianca", "salida": "2026-12-22", "reserva": "https://reserva?tfs=x&hl=es&gl=CO"}
        self.assertEqual(b.confirmar_precio(principal, v), 9_556_240)
        self.assertEqual(len(abiertas), 1)
        self.assertIsNot(abiertas[0][0], principal)
        self.assertIn("hl=en", abiertas[0][1])  # el bot lee la página en inglés

    def test_si_no_carga_lo_intenta_otra_vez_y_si_no_devuelve_none(self):
        intentos = []

        class Pagina(_Falso):
            def goto(self, url, **k):
                intentos.append(url)
                raise TimeoutError

        v = {"aerolinea": "Avianca", "salida": "2026-12-22", "reserva": "https://reserva?hl=es"}
        self.assertIsNone(b.confirmar_precio(Pagina(), v))
        self.assertEqual(len(intentos), 2)


class PruebaTope(unittest.TestCase):
    def test_justo_en_el_tope_no_avisa_y_un_peso_menos_si(self):
        self.assertIsNone(b.motivo_umbral(b.UMBRAL_TOTAL))
        self.assertIsNotNone(b.motivo_umbral(b.UMBRAL_TOTAL - 1))

    def test_tope_es_10_millones(self):
        self.assertEqual(b.UMBRAL_TOTAL, 10_000_000)


if __name__ == "__main__":
    unittest.main()
