"""Pruebas del bot con precios simulados: no abren Google ni mandan correos.

Correr con:  python -m unittest discover -s tests -v
"""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot_vuelos as b  # noqa: E402


class _Falso:
    """Hace de navegador y de página: el bot solo les llama close() y wait_for_timeout()."""

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
                           ("sync_playwright", "abrir_navegador", "buscar", "completar_reserva", "avisar", "ESTADO")}
        b.sync_playwright = _sin_playwright
        b.abrir_navegador = lambda p: (_Falso(), _Falso())
        b.buscar = self._buscar
        b.completar_reserva = self._completar
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
        # ultimo_resumen en el futuro para que el resumen semanal no se mezcle con las pruebas
        b.ESTADO.write_text(json.dumps({"revisiones": revisiones, "ultimo_resumen": "2099-01-01"}))

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

    def test_no_repite_la_misma_alerta(self):
        self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))
        self.assertEqual(self.revisar(self.umbral_total(0.94), self.umbral_total(0.98)), [])
        self.assertEqual(self.revisar(self.umbral_total(0.93), self.umbral_total(0.97)), [])  # bajó solo 1%

    def test_vuelve_a_avisar_si_baja_otro_poco(self):
        self.revisar(self.umbral_total(0.94), self.umbral_total(0.98))
        self.assertEqual(len(self.revisar(self.umbral_total(0.92), self.umbral_total(0.95))), 1)  # bajó 3%

    def test_no_avisa_si_solo_el_desde_es_bueno(self):
        self.assertEqual(self.revisar(self.umbral_total(0.95), self.umbral_total(1.07)), [])

    def test_sin_precio_real_avisa_sin_confirmar(self):
        avisos = self.revisar(self.umbral_total(0.85), None)
        self.assertEqual(len(avisos), 1)
        self.assertIn("sin confirmar", avisos[0])

    def test_bajada_fuerte_del_desde_avisa(self):
        historial = self.umbral_total(1.20)
        self.guardar_estado([{"fecha": "2026-11-01T06:00", "mejor": historial}] * b.MIN_REVISIONES_PARA_BAJADA)
        avisos = self.revisar(int(historial * 0.94), self.umbral_total(1.21))
        self.assertEqual(len(avisos), 1)

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
        self.assertIn("sin confirmar", avisos[0])

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

    def test_guarda_el_historial(self):
        self.revisar(self.umbral_total(1.3), self.umbral_total(1.4))
        self.assertEqual(len(json.loads(b.ESTADO.read_text())["revisiones"]), 1)


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
        png = b.grafica_png(estado)
        if png is None:
            self.skipTest("matplotlib no está instalado")
        self.assertTrue(png.startswith(b"\x89PNG"))



class PruebaPaginaDeReserva(unittest.TestCase):
    # Textos como los que muestra Google Flights en la página de reserva (en inglés).
    JUNTA = "Booking options\nBook with AviancaAirline\nCOP\xa09,556,240\nContinue\nView options\n"
    SEPARADOS = ("Book with LATAM, DeltaAirline\nSeparate tickets\nThe 2 tickets in this itinerary must be "
                 "booked separately\nCOP\xa014,748,866\nContinue\n")
    SEPARADOS_MISMA = "Book with LATAMAirline\nSeparate tickets\nCOP\xa08,100,000\nContinue\n"

    def test_lee_la_reserva_de_ida_y_vuelta_junta(self):
        self.assertEqual(b.precio_ida_vuelta(self.JUNTA, "Avianca"), (9_556_240, False))

    def test_ignora_tiquetes_separados_aunque_sean_mas_baratos(self):
        texto = "Booking options\n" + self.SEPARADOS_MISMA + "Book with LATAMAirline\nCOP\xa011,288,920\nContinue\n"
        self.assertEqual(b.precio_ida_vuelta(texto, "LATAM"), (11_288_920, False))

    def test_si_solo_hay_separados_no_da_precio(self):
        self.assertEqual(b.precio_ida_vuelta("Booking options\n" + self.SEPARADOS_MISMA, "LATAM"), (None, True))

    def test_no_confunde_otra_aerolinea(self):
        self.assertEqual(b.precio_ida_vuelta(self.JUNTA, "LATAM"), (None, False))


class PruebaTope(unittest.TestCase):
    def test_justo_en_el_tope_no_avisa_y_un_peso_menos_si(self):
        self.assertIsNone(b.motivo_umbral(b.UMBRAL_TOTAL))
        self.assertIsNotNone(b.motivo_umbral(b.UMBRAL_TOTAL - 1))

    def test_tope_es_10_millones(self):
        self.assertEqual(b.UMBRAL_TOTAL, 10_000_000)


if __name__ == "__main__":
    unittest.main()
