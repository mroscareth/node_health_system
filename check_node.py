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

import json
import os
import sys
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
GNOSIS_API = f"https://gnosischa.in/api/v1/validator/{GNOSIS_VALIDATORS}"
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


def check_gnosis():
    """Devuelve (estado, detalle). Estados: OK, OFFLINE, UNKNOWN."""
    try:
        r = http_json(GNOSIS_API)
        data = r.get("data")
        if data is None:
            return "UNKNOWN", "respuesta sin data de gnosischa.in"
        if isinstance(data, dict):
            data = [data]
        offline = [str(v.get("validatorindex")) for v in data
                   if "offline" in str(v.get("status", ""))]
        total = len(data)
        if offline:
            return "OFFLINE", f"{len(offline)}/{total} offline (indices: {', '.join(offline)})"
        return "OK", f"{total}/{total} validadores online"
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"error consultando gnosischa.in ({e})"


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("AVISO: faltan TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; no se envia alerta")
        print(text)
        return
    http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", {
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
    })


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
    near_state, near_detail = check_near()
    gnosis_state, gnosis_detail = check_gnosis()

    prev = load_state()
    prev_near = prev.get("near", "")
    prev_gnosis = prev.get("gnosis", "")

    lines = [
        f"{ICON[near_state]} <b>NEAR</b> ({NEAR_POOL}): {near_state} - {near_detail}",
        f"{ICON[gnosis_state]} <b>Gnosis</b>: {gnosis_state} - {gnosis_detail}",
    ]
    body = "\n".join(lines)
    print(body)

    problem = near_state not in ("OK",) or gnosis_state not in ("OK",)
    changed = (near_state != prev_near) or (gnosis_state != prev_gnosis)

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
        "near": near_state,
        "gnosis": gnosis_state,
        "last_alert": now.isoformat() if alerted else prev.get("last_alert", ""),
        "last_run_date": now.strftime("%Y-%m-%d"),  # cambia 1 vez al dia -> keepalive
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
