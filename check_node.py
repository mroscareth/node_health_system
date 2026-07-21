#!/usr/bin/env python3
"""Watchdog de validadores via datos on-chain publicos.

Revisa:
  - NEAR: que monterrey.pool.near este en el set activo y produciendo bloques/chunks.
  - Gnosis: que los validadores 363043-363045 esten online (atestando).

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
PRODUCTION_RATIO_MIN = 0.85  # debajo de esto se considera degradado
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


def check_near():
    """Devuelve (estado, detalle). Estados: OK, DEGRADED, NOT_IN_SET, UNKNOWN."""
    last_err = None
    for rpc in NEAR_RPCS:
        try:
            r = http_json(rpc, {
                "jsonrpc": "2.0", "id": "wd", "method": "validators", "params": [None],
            })
            result = r.get("result")
            if not result:
                continue
            for v in result.get("current_validators", []):
                if v.get("account_id") == NEAR_POOL:
                    prod = v.get("num_produced_blocks", 0) + v.get("num_produced_chunks", 0)
                    expc = v.get("num_expected_blocks", 0) + v.get("num_expected_chunks", 0)
                    if expc == 0:
                        return "OK", "en el set, sin bloques esperados aun en esta epoca"
                    ratio = prod / expc
                    detail = f"produccion {prod}/{expc} ({ratio:.0%})"
                    if ratio < PRODUCTION_RATIO_MIN:
                        return "DEGRADED", detail
                    return "OK", detail
            nxt = [v.get("account_id") for v in result.get("next_validators", [])]
            if NEAR_POOL in nxt:
                return "OK", "fuera del set actual pero entra en la proxima epoca"
            return "NOT_IN_SET", "no aparece en el set de validadores ni en el proximo"
        except Exception as e:  # noqa: BLE001 - probar siguiente RPC
            last_err = e
    return "UNKNOWN", f"ningun RPC de NEAR respondio ({last_err})"


def check_gnosis(prev_balance):
    """Devuelve (estado, detalle, balance_total). Estados: OK, OFFLINE, UNKNOWN.

    Un validador que atesta gana saldo; uno caido lo pierde. Se marca OFFLINE
    solo si el saldo cae por debajo del effective_balance Y sigue bajando, para
    no confundirlo con los retiros periodicos del excedente.
    """
    try:
        data = http_json(GNOSIS_API).get("data")
        if not data:
            return "UNKNOWN", "respuesta sin datos del beacon de Gnosis", None
        if isinstance(data, dict):
            data = [data]

        raros = [f"{v['index']}:{v['status']}" for v in data
                 if v.get("status") != "active_ongoing"]
        if raros:
            return "OFFLINE", "estado inesperado -> " + ", ".join(raros), None

        total = sum(int(v["balance"]) for v in data)
        efectivo = sum(int(v["validator"]["effective_balance"]) for v in data)
        gno = total / 1e9 / 32  # mGwei -> mGNO -> GNO (32 mGNO = 1 GNO)
        detalle = f"{len(data)}/{len(data)} activos, saldo {gno:.4f} GNO"

        if total < efectivo and prev_balance and total < prev_balance:
            perdida = (prev_balance - total) / 1e9 / 32
            return "OFFLINE", f"{detalle} - perdiendo saldo ({perdida:.5f} GNO desde el ultimo chequeo)", total
        return "OK", detalle, total
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"error consultando el beacon de Gnosis ({e})", None


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


ICON = {"OK": "\U0001F7E2", "DEGRADED": "\U0001F7E1", "NOT_IN_SET": "\U0001F534",
        "OFFLINE": "\U0001F534", "UNKNOWN": "⚪"}


def main():
    now = datetime.now(timezone.utc)
    prev = load_state()

    near_state, near_detail = check_near()
    gnosis_state, gnosis_detail, gnosis_balance = check_gnosis(prev.get("gnosis_balance"))

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

    problem = near_cmp in ("DEGRADED", "NOT_IN_SET") or gnosis_cmp == "OFFLINE"
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
        "gnosis_balance": gnosis_balance if gnosis_balance else prev.get("gnosis_balance"),
        "last_alert": now.isoformat() if alerted else prev.get("last_alert", ""),
        "last_run_date": now.strftime("%Y-%m-%d"),  # cambia 1 vez al dia -> keepalive
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
