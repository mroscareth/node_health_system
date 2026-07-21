# node_health_system

Watchdog externo de validadores basado en datos on-chain publicos. Corre en
GitHub Actions cada ~15 minutos y alerta por Telegram.

## Que vigila

| Red | Chequeo | Fuente |
|---|---|---|
| NEAR | `monterrey.pool.near` en el set activo y produciendo >=85% de bloques/chunks esperados | RPC publico (`validators`) |
| Gnosis | Validadores 363043-363045 en `active_ongoing` y sin perder saldo (un validador caido se penaliza cada epoca) | Beacon API de `rpc-gbc.gnosischain.com` |

Detecta tanto caidas del host como fallas de un solo servicio (p. ej. el nodo
NEAR aislado sin peers), porque mira lo que la red ve, no lo que el servidor
dice de si mismo.

## Alertas

- Cambio de estado (caida o recuperacion): mensaje inmediato.
- Problema persistente: recordatorio cada 24 h.
- Lunes ~15:00 UTC: resumen semanal (confirma que el watchdog vive).

Si una API publica no responde, el estado queda en `UNKNOWN` y **no** se alerta:
se conserva el ultimo estado conocido para evitar falsas alarmas por caidas de
los servicios consultados.

## Configuracion (Settings → Secrets and variables → Actions)

| Secret | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot (de @BotFather) |
| `TELEGRAM_CHAT_ID` | ID del chat o canal (p. ej. `-100...` para canal) |

El bot debe ser miembro/admin del canal de destino.

## Notas

- `state.json` lo escribe el workflow; guarda el ultimo estado para alertar
  solo en transiciones. Su commit diario mantiene activo el cron de GitHub
  (que se pausa tras 60 dias sin actividad).
- Prueba manual: pestana Actions → "Node watchdog" → Run workflow.
