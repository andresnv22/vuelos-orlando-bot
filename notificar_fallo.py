"""Envía una alerta mínima cuando GitHub no pudo completar una revisión.

No importa bot_vuelos ni Playwright a propósito: también debe poder avisar si falló la
instalación de Chromium o de las dependencias antes de que el bot arranque.
"""

import os
import smtplib
import sys
from email.message import EmailMessage


def main():
    usuario = os.environ.get("GMAIL_USER", "").strip()
    clave = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    destinatario = os.environ.get("EMAIL_TO", "").strip() or usuario
    if "@" not in usuario or len(clave) != 16:
        print("No se pudo avisar la falla: faltan o son inválidos los secretos de Gmail.")
        return 1

    mensaje = EmailMessage()
    mensaje["Subject"] = "⚠️ Bot vuelos BOG→MCO: falló una revisión"
    mensaje["From"] = usuario
    mensaje["To"] = destinatario
    mensaje.set_content(
        "La última ejecución programada del bot falló antes de terminar. "
        "Revisa la corrida de GitHub Actions; el bot intentará ejecutarse otra vez en la próxima franja."
    )
    for puerto in (465, 587):
        try:
            if puerto == 465:
                cliente = smtplib.SMTP_SSL("smtp.gmail.com", puerto, timeout=30)
            else:
                cliente = smtplib.SMTP("smtp.gmail.com", puerto, timeout=30)
                cliente.starttls()
            with cliente:
                cliente.login(usuario, clave)
                cliente.send_message(mensaje)
            print("Alerta de falla enviada.")
            return 0
        except Exception as error:
            print(f"Gmail (puerto {puerto}) falló: {type(error).__name__}: {error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
