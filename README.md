# ✈️ Bot de precios: Bogotá → Orlando

Vigila los vuelos **directos** de Bogotá (BOG) a Orlando (MCO) en diciembre de 2026 y avisa por correo
**solo cuando el precio de verdad vale la pena**. Corre solo en GitHub Actions, así que no depende de
que ningún computador esté prendido.

![Pruebas](https://github.com/andresnv22/vuelos-orlando-bot/actions/workflows/pruebas.yml/badge.svg)

## Qué hace

- Cada 3 horas, de 6am a 9pm hora de Colombia, busca en Google Flights. Lo despierta un
  [despertador](#despertador) a las 6, 9, 12, 3, 6 y 9 en punto. El horario propio de GitHub
  (cada media hora, solo revisa si ya pasaron más de 2h40) queda de respaldo, porque GitHub se salta muchas corridas:
  - las **fechas ideales** (22 → 27 dic) y las cercanas (hasta 2 días antes o después, de 4 a 7 noches);
  - solo vuelos **directos de Avianca y LATAM** (sin Frontier), para **4 pasajeros**, con precios en **COP**.
    El filtro va en la búsqueda misma: si Frontier entra, Google arma el "desde" de LATAM o Avianca con la
    vuelta en Frontier, que son tiquetes separados.
- Cuando el precio está bajo el tope o cerca, abre la página de reserva y lee el **precio real que da la aerolínea**
  en la línea "Book with LATAM/Avianca · Airline" (ida y vuelta en una sola compra, para los 4). No cuenta
  "Separate tickets" ni otras aerolíneas que venden los mismos vuelos (Delta vende vuelos de LATAM en dólares).
  Revisa hasta 8 fechas, empezando por las ideales, hasta encontrar una que de verdad esté bajo el tope.
  Si una fecha o un link no cargan, lo intenta una vez más.
- Manda un correo con diseño, botones para **reservar en la web oficial** de Avianca o LATAM, el link a
  **la misma búsqueda que hizo el bot** y una **gráfica** de cómo ha ido el precio.

## Cuándo llega un correo

| Correo | Cuándo |
|---|---|
| 🔥 **¡Precio bueno!** | El precio real en la aerolínea, ida y vuelta en una sola reserva para los 4, queda por debajo del tope (menos de $10.000.000). Llega en cada revisión (cada 3 horas) mientras siga así. El precio "desde" de la búsqueda nunca basta. |
| ⬆️ **Subió el precio** | Una sola vez, cuando venía avisando y el precio real ya no está bajo el tope. Después no llega nada hasta que vuelva a bajar. |
| ✈️ **Resumen del día** | A las 6pm, si ese día no salió otro aviso: el precio más barato del día, para saber que el bot sigue vivo aunque el precio esté sobre el tope. |
| ⚠️ **No se pudo revisar** / **no pude confirmar el precio real** | Google no devolvió resultados, o la página de la aerolínea no mostró el precio de una opción que en la búsqueda sale bajo el tope (máximo uno al día). |

Si un correo no se puede enviar, la corrida queda en rojo y GitHub avisa por correo a la cuenta del repositorio.

## Cambiar fechas, pasajeros o umbral

Todo está en [`config.json`](config.json); no hace falta tocar el código:

| Campo | Qué es |
|---|---|
| `pasajeros` | Número de pasajeros (desde los 12 años pagan tarifa de adulto). |
| `fechas_ideales` | Salida y regreso que de verdad quieren. |
| `flexibilidad_dias` | Cuántos días antes o después se aceptan, en la ida y en la vuelta. |
| `noches_min`, `noches_max` | Duración aceptable del viaje. |
| `aerolineas` | Aerolíneas que se buscan: código IATA y nombre como sale en Google (`AV` Avianca, `LA` LATAM). |
| `umbral_total_cop` | Tope del precio ida y vuelta **para todos**: se avisa si queda por debajo (hoy 10.000.000). |

El bot se apaga solo el día antes de la última salida posible (23 dic). Después hay que borrar el
despertador en [claude.ai/code/routines](https://claude.ai/code/routines), que sigue sonando aunque ya no haya nada que revisar.

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
- `normal`: lo mismo que la corrida programada;
- `auto`: como el horario, solo revisa si ya pasaron más de 2h40 desde la última vez.

## Despertador

El horario de GitHub (`schedule`) se salta muchas corridas: el 2/11 no hizo ninguna entre las 2am y las 3pm.
Por eso una rutina en la nube de Claude, **Despertador bot vuelos**
([claude.ai/code/routines](https://claude.ai/code/routines)), empuja un commit vacío a la rama
`claude/despertador` a las 6, 9, 12, 3, 6 y 9 en punto. Ese push lanza el workflow al instante; el workflow
siempre usa el código y el historial de `main`, y la regla de las 2h40 evita revisiones repetidas.

La rutina gasta del uso de la cuenta de Claude: si ese uso está agotado a la hora en punto, no suena
(pasó el 2/11 a las 6pm). Por eso hay un segundo despertador en el Mac,
[`mac/com.vuelos-orlando.despertador.plist`](mac/com.vuelos-orlando.despertador.plist), que 10 minutos
después (6:10, 9:10…) lanza el workflow en modo `auto`: si la rutina ya lo despertó, no hace nada. Funciona con
el Mac prendido o dormido (si estaba dormido, corre al despertar). Se instala y se quita así:

```bash
cp mac/com.vuelos-orlando.despertador.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vuelos-orlando.despertador.plist
launchctl bootout gui/$(id -u)/com.vuelos-orlando.despertador   # para quitarlo
```

El horario de GitHub queda de respaldo, aunque casi no sirve: el 2/11 corrió 2 de unas 32 veces.

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
