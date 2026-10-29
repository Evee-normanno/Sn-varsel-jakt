"""
Snøvarsel: sjekker værprognosen fra MET (samme data som Yr) for flere steder
og sender e-post når det er meldt snø. Varsler kommer i to trinn:
«tidlig varsel» for snø et stykke frem i tid, og «bekreftet» når snøen
fortsatt står i prognosen kort tid før. Hvert trinn sendes bare én gang
per sted og dato.
"""

import html
import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
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

UKEDAGER = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
UKEDAGER_LANG = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MANEDER = ["jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"]

# ---------------------------------------------------------------------------
# Henting og tolking av prognose
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


# ---------------------------------------------------------------------------
# Tilstand (hva er allerede varslet)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Hjelpefunksjoner for visning
# ---------------------------------------------------------------------------


def kort_dato(d):
    return f"{UKEDAGER[d.weekday()]} {d.day}.{d.month}"


def lang_dato(d):
    return f"{UKEDAGER_LANG[d.weekday()]} {d.day}. {MANEDER[d.month - 1]}"


def yr_lenke(sted):
    sti = quote("værvarsel/daglig-tabell")
    return f"https://www.yr.no/nb/{sti}/{sted['lat']:.4f},{sted['lon']:.4f}"


def fmt_temp(t):
    return "–" if t is None else f"{t:.0f} °C"


def celle_farge(mm):
    """Bakgrunns- og tekstfarge etter snømengde."""
    if mm < 1:
        return "#e8f2fb", "#1d4f7a"
    if mm < 5:
        return "#b9d8f2", "#123d63"
    if mm < 10:
        return "#6fa8dc", "#ffffff"
    return "#2f6aa3", "#ffffff"


# ---------------------------------------------------------------------------
# E-post: ren tekst
# ---------------------------------------------------------------------------


def lag_tekst(nye):
    deler = []
    for type_varsel, tittel in (
        ("bekreftet", f"BEKREFTET – snø innen {BEKREFTET_DAGER} dager"),
        ("tidlig", "TIDLIG VARSEL – snø lenger frem (usikkert, kan endre seg)"),
    ):
        linjer = []
        for v in nye:
            if v["type"] != type_varsel:
                continue
            linje = (
                f"• {v['sted']['navn']}: {lang_dato(v['dato'])} – "
                f"ca. {v['info']['mm']:.1f} mm (≈ {v['info']['mm']:.0f} cm), "
                f"ned mot {fmt_temp(v['info']['min_temp'])}"
            )
            if v["var_tidlig"]:
                linje += " (varslet tidlig, står fortsatt i prognosen)"
            linjer.append(linje)
        if linjer:
            deler.append(tittel + ":\n" + "\n".join(linjer))
    return (
        "\n\n".join(deler)
        + "\n\nSe detaljer på yr.no.\nData fra Meteorologisk institutt (CC BY 4.0)."
    )


# ---------------------------------------------------------------------------
# E-post: HTML
# ---------------------------------------------------------------------------

FONT = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


def lag_oversikt_html(oversikt, nye_nokler, i_dag):
    """Rutenett: steder nedover, dager bortover, snømengde i hver rute."""
    datoer = [i_dag + timedelta(days=i) for i in range(DAGER_FREMOVER + 1)]

    hode = (
        f'<th style="padding:8px 10px;text-align:left;font-size:12px;color:#5b6b7a;'
        f'border-bottom:2px solid #d5e2ee;">Sted</th>'
    )
    for i, d in enumerate(datoer):
        kort_sikt = i <= BEKREFTET_DAGER
        bg = "#f3f8fd" if kort_sikt else "#ffffff"
        hode += (
            f'<th style="padding:8px 4px;text-align:center;font-size:12px;color:#5b6b7a;'
            f'background:{bg};border-bottom:2px solid #d5e2ee;white-space:nowrap;">'
            f"{UKEDAGER[d.weekday()]}<br>"
            f'<span style="font-weight:normal;">{d.day}.{d.month}</span></th>'
        )

    rader = ""
    for sted in STEDER:
        navn = sted["navn"]
        rad = (
            f'<td style="padding:8px 10px;font-size:14px;font-weight:600;color:#1f2d3a;'
            f'border-bottom:1px solid #e6edf3;white-space:nowrap;">'
            f'<a href="{yr_lenke(sted)}" style="color:#1f2d3a;text-decoration:none;">'
            f"{html.escape(navn)}</a></td>"
        )
        for i, d in enumerate(datoer):
            info = oversikt.get(navn, {}).get(d.isoformat())
            kort_sikt = i <= BEKREFTET_DAGER
            tom_bg = "#f3f8fd" if kort_sikt else "#ffffff"
            if info is None:
                rad += (
                    f'<td style="padding:6px 4px;text-align:center;color:#c2ccd6;'
                    f'background:{tom_bg};border-bottom:1px solid #e6edf3;">·</td>'
                )
                continue
            bg, fg = celle_farge(info["mm"])
            ny = (navn, d.isoformat()) in nye_nokler
            ramme = "border:2px solid #e0712c;" if ny else "border-bottom:1px solid #e6edf3;"
            rad += (
                f'<td style="padding:6px 4px;text-align:center;font-size:13px;font-weight:600;'
                f'background:{bg};color:{fg};{ramme}">{info["mm"]:.1f}</td>'
            )
        rader += f"<tr>{rad}</tr>"

    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        f'style="border-collapse:collapse;font-family:{FONT};">'
        f"<tr>{hode}</tr>{rader}</table>"
        f'<p style="margin:10px 0 0;font-size:12px;color:#5b6b7a;line-height:1.6;">'
        f"Tallene er mm nedbør som snø (≈ cm nysnø). "
        f'<span style="display:inline-block;width:10px;height:10px;border:2px solid #e0712c;'
        f'vertical-align:middle;"></span> = nytt i denne e-posten. '
        f"De første {BEKREFTET_DAGER + 1} kolonnene (lys bakgrunn) er kort sikt. "
        f"Klikk på et stedsnavn for å åpne Yr.</p>"
    )


def lag_detaljer_html(nye, type_varsel):
    rader = [v for v in nye if v["type"] == type_varsel]
    if not rader:
        return ""
    if type_varsel == "bekreftet":
        tittel, farge, merke = f"Bekreftet – snø innen {BEKREFTET_DAGER} dager", "#2f6aa3", "❄️"
        undertekst = "Snøen står fortsatt i prognosen kort tid før."
    else:
        tittel, farge, merke = "Tidlig varsel – snø lenger frem", "#8a6d1f", "🌨️"
        undertekst = "Usikkert – prognosen kan endre seg de neste dagene."

    cell = "padding:8px 10px;font-size:14px;border-bottom:1px solid #e6edf3;color:#1f2d3a;"
    th = (
        "padding:8px 10px;font-size:12px;text-align:left;color:#5b6b7a;"
        "border-bottom:2px solid #d5e2ee;"
    )
    linjer = ""
    for v in sorted(rader, key=lambda v: (v["dato"], v["sted"]["navn"])):
        info = v["info"]
        notat = (
            '<br><span style="font-size:12px;color:#5b6b7a;">varslet tidlig, står fortsatt</span>'
            if v["var_tidlig"] else ""
        )
        linjer += (
            f"<tr>"
            f'<td style="{cell}font-weight:600;">{html.escape(v["sted"]["navn"])}{notat}</td>'
            f'<td style="{cell}white-space:nowrap;">{lang_dato(v["dato"])}</td>'
            f'<td style="{cell}white-space:nowrap;">{info["mm"]:.1f} mm '
            f'<span style="color:#5b6b7a;">(≈ {info["mm"]:.0f} cm)</span></td>'
            f'<td style="{cell}white-space:nowrap;">{fmt_temp(info["min_temp"])}</td>'
            f'<td style="{cell}white-space:nowrap;"><a href="{yr_lenke(v["sted"])}" '
            f'style="color:#2f6aa3;text-decoration:none;font-weight:600;">Yr →</a></td>'
            f"</tr>"
        )

    return (
        f'<h2 style="margin:28px 0 4px;font-size:17px;color:{farge};font-family:{FONT};">'
        f"{merke} {tittel}</h2>"
        f'<p style="margin:0 0 10px;font-size:13px;color:#5b6b7a;font-family:{FONT};">'
        f"{undertekst}</p>"
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        f'style="border-collapse:collapse;font-family:{FONT};">'
        f'<tr><th style="{th}">Sted</th><th style="{th}">Dag</th><th style="{th}">Snø</th>'
        f'<th style="{th}">Laveste</th><th style="{th}"></th></tr>'
        f"{linjer}</table>"
    )


def lag_html(oversikt, nye, i_dag):
    antall_steder = len({v["sted"]["navn"] for v in nye})
    antall_bekreftet = sum(1 for v in nye if v["type"] == "bekreftet")
    antall_tidlig = len(nye) - antall_bekreftet

    sammendrag = []
    if antall_bekreftet:
        sammendrag.append(f"{antall_bekreftet} bekreftet")
    if antall_tidlig:
        sammendrag.append(f"{antall_tidlig} tidlig{'e' if antall_tidlig > 1 else ''}")
    stedtekst = "1 sted" if antall_steder == 1 else f"{antall_steder} steder"

    nye_nokler = {(v["sted"]["navn"], v["dato"].isoformat()) for v in nye}
    oppdatert = datetime.now(TIDSSONE)

    return f"""\
<!DOCTYPE html>
<html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
</head>
<body style="margin:0;padding:0;background:#eef3f8;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#eef3f8;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden;
              font-family:{FONT};">

<tr><td style="background:#1f4e79;padding:24px 28px;">
  <div style="font-size:24px;font-weight:700;color:#ffffff;">❄️ Snøvarsel</div>
  <div style="font-size:14px;color:#cfe2f3;margin-top:6px;">
    Ny snø i prognosen for {stedtekst} · {" og ".join(sammendrag)}
  </div>
</td></tr>

<tr><td style="padding:24px 28px 8px;">
  <h2 style="margin:0 0 12px;font-size:17px;color:#1f2d3a;">Oversikt neste {DAGER_FREMOVER} dager</h2>
  <div style="overflow-x:auto;">{lag_oversikt_html(oversikt, nye_nokler, i_dag)}</div>
  {lag_detaljer_html(nye, "bekreftet")}
  {lag_detaljer_html(nye, "tidlig")}
</td></tr>

<tr><td style="padding:20px 28px 26px;font-size:12px;color:#8696a5;line-height:1.6;
               border-top:1px solid #e6edf3;">
  Sjekket {lang_dato(oppdatert.date())} kl. {oppdatert:%H:%M}.
  Snømengde er nedbør i mm, der 1 mm ≈ 1 cm nysnø.<br>
  Data fra Meteorologisk institutt (CC BY 4.0) – samme prognose som på yr.no.
</td></tr>

</table></td></tr></table></body></html>"""


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_epost(emne, tekst, html_innhold=None):
    bruker = os.environ["SMTP_USER"]
    passord = os.environ["SMTP_PASS"]
    mottaker = os.environ.get("MAIL_TO", bruker)
    vert = os.environ.get("SMTP_HOST", "smtp.gmail.com")

    melding = EmailMessage()
    melding["Subject"] = emne
    melding["From"] = f"Snøvarsel <{bruker}>"
    melding["To"] = mottaker
    melding.set_content(tekst)
    if html_innhold:
        melding.add_alternative(html_innhold, subtype="html")

    with smtplib.SMTP_SSL(vert, 465) as server:
        server.login(bruker, passord)
        server.send_message(melding)


# ---------------------------------------------------------------------------
# Hovedprogram
# ---------------------------------------------------------------------------


def main():
    tilstand = les_tilstand()
    i_dag = datetime.now(TIDSSONE).date()
    naa = datetime.now(TIDSSONE).isoformat(timespec="minutes")
    oversikt = {}   # {stedsnavn: {dato: info}} – all snø i prognosen
    nye = []        # varsler som ikke er sendt før

    for sted in STEDER:
        try:
            prognose = hent_prognose(sted["lat"], sted["lon"])
        except Exception as feil:
            print(f"Klarte ikke hente prognose for {sted['navn']}: {feil}", file=sys.stderr)
            continue

        snodager = finn_snodager(prognose)
        oversikt[sted["navn"]] = snodager

        for dato_str, info in sorted(snodager.items()):
            dato = date.fromisoformat(dato_str)
            type_varsel = "bekreftet" if (dato - i_dag).days <= BEKREFTET_DAGER else "tidlig"
            nokkel = f"{sted['navn']}|{dato_str}|{type_varsel}"
            if nokkel in tilstand:
                continue
            tilstand[nokkel] = naa
            nye.append({
                "sted": sted,
                "dato": dato,
                "info": info,
                "type": type_varsel,
                "var_tidlig": type_varsel == "bekreftet"
                and f"{sted['navn']}|{dato_str}|tidlig" in tilstand,
            })

    if nye:
        har_bekreftet = any(v["type"] == "bekreftet" for v in nye)
        emne = "❄️ Snø bekreftet!" if har_bekreftet else "🌨️ Tidlig snøvarsel"
        tekst = lag_tekst(nye)
        print(tekst)
        send_epost(emne, tekst, lag_html(oversikt, nye, i_dag))
    else:
        print("Ingen ny snø i prognosen.")

    lagre_tilstand(tilstand)


def send_test():
    """Sender en eksempel-e-post med oppdiktet snø, så du ser hvordan den ser ut."""
    i_dag = datetime.now(TIDSSONE).date()
    eksempel = [(0, 1, 2.4, -3), (0, 4, 6.1, -6), (1, 2, 0.6, -1), (2, 5, 3.0, -4),
                (3, 1, 8.5, -7), (3, 2, 12.3, -9), (4, 0, 4.2, -11), (4, 3, 15.0, -14)]
    oversikt, nye = {}, []
    for s_idx, dag, mm, temp in eksempel:
        if s_idx >= len(STEDER):
            continue
        sted = STEDER[s_idx]
        dato = i_dag + timedelta(days=dag)
        info = {"mm": mm, "min_temp": temp}
        oversikt.setdefault(sted["navn"], {})[dato.isoformat()] = info
        nye.append({
            "sted": sted, "dato": dato, "info": info,
            "type": "bekreftet" if dag <= BEKREFTET_DAGER else "tidlig",
            "var_tidlig": dag == 1,
        })
    send_epost(
        "✅ Test fra Snøvarsel (eksempeldata)",
        "TEST – dette er oppdiktede tall, ikke en ekte prognose.\n\n" + lag_tekst(nye),
        lag_html(oversikt, nye, i_dag).replace(
            "❄️ Snøvarsel</div>",
            "❄️ Snøvarsel <span style=\"font-size:13px;background:#e0712c;"
            "padding:3px 8px;border-radius:4px;vertical-align:middle;\">TEST</span></div>",
        ),
    )
    print("Test-e-post sendt.")


if __name__ == "__main__":
    if os.environ.get("TEST_EPOST") == "true":
        send_test()
    else:
        main()
