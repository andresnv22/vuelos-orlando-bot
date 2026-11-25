"""Comprueba que la alerta de un fallo puede salir sin instalar Playwright."""

import os
import unittest
from unittest.mock import patch

import notificar_fallo


class SMTPFalso:
    enviados = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def login(self, usuario, clave):
        pass

    def send_message(self, mensaje):
        self.enviados.append(mensaje)


class PruebaNotificacionDeFallo(unittest.TestCase):
    def test_envia_alerta_al_destinatario_configurado(self):
        SMTPFalso.enviados = []
        entorno = {"GMAIL_USER": "origen@gmail.com", "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
                   "EMAIL_TO": "anddresnv22@gmail.com"}
        with patch.dict(os.environ, entorno), patch.object(notificar_fallo.smtplib, "SMTP_SSL", SMTPFalso):
            self.assertEqual(notificar_fallo.main(), 0)
        self.assertEqual(len(SMTPFalso.enviados), 1)
        self.assertEqual(SMTPFalso.enviados[0]["To"], "anddresnv22@gmail.com")


if __name__ == "__main__":
    unittest.main()
