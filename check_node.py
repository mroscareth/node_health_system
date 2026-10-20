#!/usr/bin/env python3
"""Watchdog de validadores via datos on-chain publicos.

Revisa:
  - NEAR: que monterrey.pool.near este en el set activo y con su ratio de
    endorsements por encima del umbral de pago (comparado con la red).
  - Gnosis: que los validadores 363043-363045 esten online (atestando),
    sin confundir el barrido automatico de retiros con una caida.

Alerta por Telegram solo en cambios de estado (con recordatorio cada 24h si
sigue mal) y manda un resumen semanal los lunes para confirmar que el
watchdog mismo sigue vivo. Estado persistido en state.json (commiteado por el
workflow; sirve tambien de keepalive del cron de GitHub).
"""

import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEAR_POOL = "monterrey.pool.near"
NEAR_RPCS = [
    "https://rpc.mainnet.near.org",
    "https://rpc.mainnet.fastnear.com",
    "https://1rpc.io/near",
]
GNOSIS_VALIDATORS = "363043,363044,363045"
GNOSIS_API = ("https://rpc-gbc.gnosischain.com/eth/v1/beacon/states/head/validators"
              f"?id={GNOSIS_VALIDATORS}")
# NEAR: bajo 90% de endorsements la epoca paga cero
NEAR_RATIO_MIN = 0.90
NEAR_GAP_VS_MEDIAN = 0.05   # y ademas 5 puntos bajo la mediana de la red
NEAR_MIN_EXPECTED = 2000    # no juzgar con muy pocos datos (~6% de epoca)
NEAR_NETWORK_HEALTHY = 0.97  # mediana normal de la red: ~99.5%
# Gnosis: el barrido de retiros parciales deja cada saldo en 32 + lo ganado desde
# entonces. Una bajada que cae en esta franja es un retiro, no una penalizacion.
GNOSIS_MAX_EFFECTIVE = 32_000_000_000
GNOSIS_SWEEP_BAND = 5_000_000          # 0.005 de margen (~1 dia de premios)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
REALERT_HOURS = 24

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def http_json(url, payload=None, timeout=25):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _work(v):
    """(producido, esperado) sumando bloques, chunks y endorsements.

    El pool es chunk validator: no produce bloques ni chunks, su trabajo son los
    endorsements, y su ratio es lo que decide el pago (<90% = cero esa epoca).
    """
    keys = ("blocks", "chunks", "endorsements")
    prod = sum(v.get(f"num_produced_{k}", 0) for k in keys)
    expc = sum(v.get(f"num_expected_{k}", 0) for k in keys)
    return prod, expc


def check_near():
    """Devuelve (estado, detalle).

    Estados: OK, DEGRADED, OUT_THIS_EPOCH, NOT_IN_SET, UNKNOWN.

    DEGRADED solo si el ratio cae bajo el umbral de pago Y queda claramente por
    debajo de la mediana de la red: cuando un shard se congestiona, todos los
    validadores bajan a la vez y eso no es una falla de nuestro nodo.
    """
    last_err = None
    for rpc in NEAR_RPCS:
        try:
            r = http_json(rpc, {
                "jsonrpc": "2.0", "id": "wd", "method": "validators", "params": [None],
            })
            result = r.get("result")
            if not result:
                continue
            current = result.get("current_validators", [])
            ratios = sorted(p / e for p, e in map(_work, current) if e > 0)
            median = ratios[len(ratios) // 2] if ratios else None
            for v in current:
                if v.get("account_id") != NEAR_POOL:
                    continue
                prod, expc = _work(v)
                if expc < NEAR_MIN_EXPECTED:
                    return "OK", f"en el set, aun pocos datos en esta epoca ({prod}/{expc})"
                ratio = prod / expc
                detail = f"endorsements {ratio:.2%} en esta epoca"
                if median is not None:
                    detail += f" (mediana de la red {median:.2%})"
                if (ratio < NEAR_RATIO_MIN and median is not None
                        and median - ratio > NEAR_GAP_VS_MEDIAN):
                    detail += " - bajo el umbral de pago"
                    if median < NEAR_NETWORK_HEALTHY:
                        detail += "; la red tambien esta congestionada"
                    return "DEGRADED", detail
                return "OK", detail
            nxt = [v.get("account_id") for v in result.get("next_validators", [])]
            if NEAR_POOL in nxt:
                return ("OUT_THIS_EPOCH",
                        "fuera del set esta epoca (no cobra); ya figura en la proxima")
            return "NOT_IN_SET", "no aparece en el set de validadores ni en el proximo"
        except Exception as e:  # noqa: BLE001 - probar siguiente RPC
            last_err = e
    return "UNKNOWN", f"ningun RPC de NEAR respondio ({last_err})"


def _swept(balance):
    """True si el saldo quedo justo sobre 32: huella del barrido de retiros."""
    return GNOSIS_MAX_EFFECTIVE <= balance <= GNOSIS_MAX_EFFECTIVE + GNOSIS_SWEEP_BAND


def check_gnosis(prev_balances, dec_streak):
    """Devuelve (estado, detalle, saldos_por_validador, racha_de_bajadas).

    Un validador EN LINEA gana saldo cada epoca; uno CAIDO lo pierde. Se compara
    cada validador contra el chequeo anterior. Una bajada que deja el saldo en
    ~32.000x es el barrido automatico de retiros (credenciales 0x01), no una
    penalizacion: los validadores consecutivos se barren casi juntos, y antes eso
    disparaba falsas alarmas. Se marca OFFLINE tras DOS chequeos seguidos con
    alguna bajada real. Estados: OK, OFFLINE, UNKNOWN.
    """
    try:
        data = http_json(GNOSIS_API).get("data")
        if not data:
            return "UNKNOWN", "respuesta sin datos del beacon de Gnosis", None, dec_streak
        if isinstance(data, dict):
            data = [data]

        raros = [f"{v['index']}:{v['status']}" for v in data
                 if v.get("status") != "active_ongoing"]
        if raros:
            # exited / slashed / pending: falla inmediata, no depende del saldo
            return "OFFLINE", "estado inesperado -> " + ", ".join(raros), None, 0

        balances = {str(v["index"]): int(v["balance"]) for v in data}
        total = sum(balances.values())
        gno = total / 1e9 / 32  # mGwei -> mGNO -> GNO (32 mGNO = 1 GNO)
        detalle = f"{len(data)}/{len(data)} activos, saldo {gno:.4f} GNO"

        # margen de ruido: ignora variaciones minimas (~1 epoca de premio)
        NOISE = 200_000  # en las mismas unidades del beacon (~1e-7 GNO)
        prev_balances = prev_balances or {}
        perdidos, barridos, perdida = [], [], 0
        for idx, bal in balances.items():
            prev = prev_balances.get(idx)
            if prev is None or bal >= prev - NOISE:
                continue
            if _swept(bal):
                barridos.append(idx)
            else:
                perdidos.append(idx)
                perdida += prev - bal

        if perdidos:
            streak = dec_streak + 1
            quienes = ", ".join(perdidos)
            gno_perdido = perdida / 1e9 / 32
            if streak >= 2:
                return ("OFFLINE",
                        f"{detalle} - saldo bajando {streak} chequeos seguidos "
                        f"({quienes}; -{gno_perdido:.5f} GNO el ultimo)", balances, streak)
            return ("OK", f"{detalle} - saldo bajo una vez en {quienes} (vigilando)",
                    balances, streak)
        if barridos:
            return ("OK", f"{detalle} - retiro automatico de recompensas "
                          f"({', '.join(barridos)})", balances, 0)
        return "OK", detalle, balances, 0
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"error consultando el beacon de Gnosis ({e})", None, dec_streak


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("AVISO: faltan TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; no se envia alerta")
        print(text)
        return
    try:
        http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", {
            "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR Telegram: HTTP {e.code} - {body}")
        print("Revisa los secrets: TELEGRAM_BOT_TOKEN (formato 123456789:AA..., "
              "sin espacios ni comillas) y TELEGRAM_CHAT_ID (solo el numero).")
        sys.exit(1)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


ICON = {"OK": "\U0001F7E2", "DEGRADED": "\U0001F7E1", "OUT_THIS_EPOCH": "\U0001F7E1",
        "NOT_IN_SET": "\U0001F534", "OFFLINE": "\U0001F534", "UNKNOWN": "⚪"}


def main():
    now = datetime.now(timezone.utc)
    prev = load_state()

    near_state, near_detail = check_near()
    gnosis_state, gnosis_detail, gnosis_balances, gnosis_streak = check_gnosis(
        prev.get("gnosis_balances"), prev.get("gnosis_dec_streak", 0))

    # UNKNOWN = no pudimos consultar; no es una falla del nodo. Se conserva el
    # ultimo estado conocido para no alertar por caidas de las APIs publicas.
    prev_near = prev.get("near", "")
    prev_gnosis = prev.get("gnosis", "")
    near_cmp = prev_near if near_state == "UNKNOWN" and prev_near else near_state
    gnosis_cmp = prev_gnosis if gnosis_state == "UNKNOWN" and prev_gnosis else gnosis_state

    lines = [
        f"{ICON[near_state]} <b>NEAR</b> ({NEAR_POOL}): {near_state} - {html.escape(near_detail)}",
        f"{ICON[gnosis_state]} <b>Gnosis</b>: {gnosis_state} - {html.escape(gnosis_detail)}",
    ]
    body = "\n".join(lines)
    print(body)

    problem = (near_cmp in ("DEGRADED", "OUT_THIS_EPOCH", "NOT_IN_SET")
               or gnosis_cmp == "OFFLINE")
    changed = (near_cmp != prev_near) or (gnosis_cmp != prev_gnosis)

    last_alert = prev.get("last_alert")
    hours_since_alert = REALERT_HOURS + 1
    if last_alert:
        try:
            hours_since_alert = (now - datetime.fromisoformat(last_alert)).total_seconds() / 3600
        except ValueError:
            pass

    alerted = False
    if changed and prev:  # cambios reales (no primer arranque silencioso)
        title = "⚠️ Cambio de estado en el nodo" if problem else "✅ Nodo recuperado"
        send_telegram(f"<b>{title}</b>\n{body}")
        alerted = True
    elif changed and not prev:  # primer arranque: reportar estado inicial
        send_telegram(f"<b>\U0001F415 Watchdog activado - estado inicial</b>\n{body}")
        alerted = True
    elif problem and hours_since_alert >= REALERT_HOURS:
        send_telegram(f"<b>⏰ Recordatorio: el problema persiste</b>\n{body}")
        alerted = True
    elif now.weekday() == 0 and now.hour == 15 and now.minute < 20:
        send_telegram(f"<b>\U0001F4CB Resumen semanal del watchdog</b>\n{body}")

    state = {
        "near": near_cmp,
        "gnosis": gnosis_cmp,
        "gnosis_balances": gnosis_balances if gnosis_balances else prev.get("gnosis_balances"),
        "gnosis_dec_streak": gnosis_streak,
        "last_alert": now.isoformat() if alerted else prev.get("last_alert", ""),
        "last_run_date": now.strftime("%Y-%m-%d"),  # cambia 1 vez al dia -> keepalive
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
