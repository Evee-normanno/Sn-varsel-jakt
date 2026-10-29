"""
Snøvarsel: sjekker værprognosen fra MET (samme data som Yr) for tre steder
og sender e-post når det er meldt snø. Hver kombinasjon av sted og dato
varsles bare én gang, slik at du ikke får samme e-post flere ganger.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# INNSTILLINGER – endre her ved behov
# ---------------------------------------------------------------------------

# Koordinatene er omtrentlige. Kontroller dem gjerne på norgeskart.no
# (høyreklikk på stedet for å se koordinatene). Maks 4 desimaler.
STEDER = [
    {"navn": "Skramstadsætra", "lat": 61.15, "lon": 11.15},
    {"navn": "Holleia", "lat": 60.15, "lon": 10.00},
    {"navn": "Meitsjøen", "lat": 60.3833, "lon": 11.8167},
    {"navn": "Ryskdalen", "lat": 61.3206, "lon": 12.6080},
    {"navn": "Galdhøpiggen", "lat": 61.6364, "lon": 8.3125},
]

DAGER_FREMOVER = 7        # Hvor langt frem i prognosen vi ser etter snø
BEKREFTET_DAGER = 2       # Snø innen så mange dager gir «bekreftet varsel»;
                          # snø lenger frem gir «tidlig varsel»
MIN_NEDBOR_MM = 0.1       # Minste nedbør (mm) i en periode for å telle som snø
TA_MED_SLUDD = False      # Sett til True hvis sludd også skal gi varsel

# MET krever at du identifiserer deg med navn på appen og kontaktinfo.
KONTAKT = os.environ.get("MET_KONTAKT", "snovarsel/1.0 ukjent@example.com")

TILSTANDSFIL = Path(__file__).with_name("varslet.json")
TIDSSONE = ZoneInfo("Europe/Oslo")
API = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

# ---------------------------------------------------------------------------


def hent_prognose(lat, lon):
    url = f"{API}?lat={lat:.4f}&lon={lon:.4f}"
    req = Request(url, headers={"User-Agent": KONTAKT})
    with urlopen(req, timeout=30) as svar:
        return json.load(svar)


def er_sno(symbol):
    if "snow" in symbol:
        return True
    return TA_MED_SLUDD and "sleet" in symbol


def finn_snodager(prognose):
    """Returnerer {dato: {"mm": sum nedbør, "min_temp": laveste temp}} for dager med snø."""
    grense = datetime.now(timezone.utc) + timedelta(days=DAGER_FREMOVER)
    dager = {}
    for punkt in prognose["properties"]["timeseries"]:
        tid = datetime.fromisoformat(punkt["time"].replace("Z", "+00:00"))
        if tid > grense:
            break
        data = punkt["data"]
        # Bruk timesprognose der den finnes, ellers 6-timersprognose
        periode = data.get("next_1_hours") or data.get("next_6_hours")
        if not periode:
            continue
        symbol = periode["summary"]["symbol_code"]
        mm = periode.get("details", {}).get("precipitation_amount", 0.0)
        if not er_sno(symbol) or mm < MIN_NEDBOR_MM:
            continue
        dato = tid.astimezone(TIDSSONE).date().isoformat()
        temp = data["instant"]["details"].get("air_temperature")
        dag = dager.setdefault(dato, {"mm": 0.0, "min_temp": temp})
        dag["mm"] += mm
        if temp is not None and (dag["min_temp"] is None or temp < dag["min_temp"]):
            dag["min_temp"] = temp
    return dager


def les_tilstand():
    try:
        return json.loads(TILSTANDSFIL.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def lagre_tilstand(tilstand):
    # Rydd bort datoer som har passert
    i_dag = datetime.now(TIDSSONE).date().isoformat()
    tilstand = {k: v for k, v in tilstand.items() if k.split("|")[1] >= i_dag}
    TILSTANDSFIL.write_text(json.dumps(tilstand, indent=2, ensure_ascii=False), encoding="utf-8")


def send_epost(emne, tekst):
    bruker = os.environ["SMTP_USER"]
    passord = os.environ["SMTP_PASS"]
    mottaker = os.environ.get("MAIL_TO", bruker)
    vert = os.environ.get("SMTP_HOST", "smtp.gmail.com")

    melding = EmailMessage()
    melding["Subject"] = emne
    melding["From"] = bruker
    melding["To"] = mottaker
    melding.set_content(tekst)

    with smtplib.SMTP_SSL(vert, 465) as server:
        server.login(bruker, passord)
        server.send_message(melding)


def main():
    tilstand = les_tilstand()
    i_dag = datetime.now(TIDSSONE).date()
    naa = datetime.now(TIDSSONE).isoformat(timespec="minutes")
    bekreftede, tidlige = [], []

    for sted in STEDER:
        try:
            prognose = hent_prognose(sted["lat"], sted["lon"])
        except Exception as feil:
            print(f"Klarte ikke hente prognose for {sted['navn']}: {feil}", file=sys.stderr)
            continue

        for dato, info in sorted(finn_snodager(prognose).items()):
            dager_unna = (datetime.fromisoformat(dato).date() - i_dag).days
            type_varsel = "bekreftet" if dager_unna <= BEKREFTET_DAGER else "tidlig"
            nokkel = f"{sted['navn']}|{dato}|{type_varsel}"
            if nokkel in tilstand:
                continue
            tilstand[nokkel] = naa

            temp = f", ned mot {info['min_temp']:.0f} °C" if info["min_temp"] is not None else ""
            linje = f"• {sted['navn']}: {dato} – ca. {info['mm']:.1f} mm{temp}"
            if type_varsel == "bekreftet":
                if f"{sted['navn']}|{dato}|tidlig" in tilstand:
                    linje += " (varslet tidlig, står fortsatt i prognosen)"
                bekreftede.append(linje)
            else:
                tidlige.append(linje)

    if bekreftede or tidlige:
        deler = []
        if bekreftede:
            deler.append(
                f"BEKREFTET – snø innen {BEKREFTET_DAGER} dager:\n" + "\n".join(bekreftede)
            )
        if tidlige:
            deler.append(
                "TIDLIG VARSEL – snø lenger frem (usikkert, kan endre seg):\n"
                + "\n".join(tidlige)
            )
        tekst = (
            "\n\n".join(deler)
            + "\n\nSe detaljer på yr.no.\n"
            "Data fra Meteorologisk institutt (CC BY 4.0)."
        )
        emne = "❄️ Snø bekreftet!" if bekreftede else "🌨️ Tidlig snøvarsel"
        print(tekst)
        send_epost(emne, tekst)
    else:
        print("Ingen ny snø i prognosen.")

    lagre_tilstand(tilstand)


if __name__ == "__main__":
    if os.environ.get("TEST_EPOST") == "true":
        steder = ", ".join(s["navn"] for s in STEDER)
        send_epost(
            "✅ Test fra Snøvarsel",
            "Dette er en test. E-postoppsettet fungerer!\n\n"
            f"Stedene som overvåkes: {steder}.",
        )
        print("Test-e-post sendt.")
    else:
        main()
