# ✈️ Bot de precios: Bogotá → Orlando

Vigila los vuelos **directos** de Bogotá (BOG) a Orlando (MCO) en diciembre de 2026 y avisa por correo
**solo cuando el precio de verdad vale la pena**. Corre solo en GitHub Actions, así que no depende de
que ningún computador esté prendido.

![Pruebas](https://github.com/andresnv22/vuelos-orlando-bot/actions/workflows/pruebas.yml/badge.svg)

## Qué hace

- Cada 3 horas (6am, 9am, 12m, 3pm, 6pm y 9pm, hora de Colombia) busca en Google Flights:
  - las **fechas ideales** (22 → 27 dic) y las cercanas (hasta 2 días antes o después, de 4 a 7 noches);
  - solo vuelos **directos**, sin Frontier, para **4 pasajeros**, con precios en **COP**.
- Cuando el precio parece bueno, abre la página de reserva y lee el **precio real que da la aerolínea**
  (suele ser más alto que el "desde" de la búsqueda).
- Manda un correo con diseño, botones para **reservar en la web oficial** de Avianca o LATAM y una
  **gráfica** de cómo ha ido el precio.

## Cuándo llega un correo

| Correo | Cuándo |
|---|---|
| 🔥 **¡Precio bueno!** | El precio real en la aerolínea, ida y vuelta para los 4, queda por debajo del tope (menos de $10.000.000), o el precio "desde" cae 5% bajo el mínimo visto. No se repite salvo que baje otro 2%. |
| 🔥 **Posible precio bueno (sin confirmar)** | Igual que el anterior, pero no se pudo leer el precio en la aerolínea. |
| ✈️ **Resumen semanal** | Los lunes, para saber que el bot sigue vivo. |
| ⚠️ **No se pudo revisar** | Google no devolvió resultados (máximo uno al día). |

Si un correo no se puede enviar, la corrida queda en rojo y GitHub avisa por correo a la cuenta del repositorio.

## Cambiar fechas, pasajeros o umbral

Todo está en [`config.json`](config.json); no hace falta tocar el código:

| Campo | Qué es |
|---|---|
| `pasajeros` | Número de pasajeros (desde los 12 años pagan tarifa de adulto). |
| `fechas_ideales` | Salida y regreso que de verdad quieren. |
| `flexibilidad_dias` | Cuántos días antes o después se aceptan, en la ida y en la vuelta. |
| `noches_min`, `noches_max` | Duración aceptable del viaje. |
| `excluir_aerolineas` | Aerolíneas que no interesan. |
| `umbral_total_cop` | Tope del precio ida y vuelta **para todos**: se avisa si queda por debajo (hoy 10.000.000). |
| `bajada_fuerte` | Caída contra el mínimo visto que también dispara aviso (0.05 = 5%). |
| `realerta_si_baja` | Cuánto más tiene que bajar para volver a avisar (0.02 = 2%). |

El bot se apaga solo el día antes de la última salida posible.

## Secretos de GitHub

En **Settings → Secrets and variables → Actions**:

| Secreto | Qué es |
|---|---|
| `GMAIL_USER` | Cuenta de Gmail que envía los correos. |
| `GMAIL_APP_PASSWORD` | [Contraseña de aplicación](https://myaccount.google.com/apppasswords) de **esa misma** cuenta (16 letras). |
| `EMAIL_TO` | *Opcional.* A dónde llegan los avisos; si no existe, llegan a `GMAIL_USER`. |

## Probar a mano

En la pestaña **Actions → Bot vuelos BOG-MCO → Run workflow** se puede elegir:

- `prueba`: revisa 2 fechas y manda un correo de prueba con precios y links reales;
- `resumen`: hace la revisión completa y manda el resumen;
- `normal`: lo mismo que la corrida programada.

## Desarrollo

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m unittest discover -s tests -v   # pruebas con precios simulados
python bot_vuelos.py --dry-run             # revisión real sin enviar nada
```

Archivos:

- `bot_vuelos.py`: el bot.
- `config.json`: fechas, pasajeros y umbral.
- `estado.json`: historial de precios; lo actualiza el bot en cada revisión.
- `tests/`: pruebas automáticas; GitHub las corre en cada cambio.
- `.github/workflows/`: horario del bot (`bot.yml`) y pruebas (`pruebas.yml`).

Los precios vienen de Google Flights, que muestra las tarifas publicadas por las aerolíneas. Compra siempre en
la web oficial de la aerolínea.
