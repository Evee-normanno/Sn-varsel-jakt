"""
Snøvarsel: sjekker værprognosen fra MET (samme data som Yr) for flere steder
og sender e-post når det er meldt snø. E-posten viser også vind for alle steder,
og hvilke dager som har gode forhold for toppjakt (nysnø og rolig vind). Varsler kommer i to trinn:
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
]

DAGER_FREMOVER = 9        # Hvor langt frem i prognosen vi ser (Yr viser ca. 9 dager)
BEKREFTET_DAGER = 2       # Snø innen så mange dager gir «bekreftet varsel»;
                          # snø lenger frem gir «tidlig varsel»
MIN_NEDBOR_MM = 0.1       # Minste nedbør (mm) i en periode for å telle som snø
TA_MED_SLUDD = False      # Sett til True hvis sludd også skal gi varsel

# Jaktforhold (toppjakt etter tiur og orrfugl): en dag regnes som god når det
# har kommet snø de siste dagene før, og det er rolig vind på dagtid.
JAKT_MAKS_KAST = 6.0      # Høyeste vindkast (m/s) på dagtid for gode forhold
JAKT_MIN_SNO_MM = 1.0     # Minst så mye nysnø (mm ≈ cm) før dagen
JAKT_DAGER_ETTER_SNO = 2  # Snøfall så mange dager tilbake teller
JAKT_FRA_KL = 6           # Dagtid som vinden vurderes for (lokal tid)
JAKT_TIL_KL = 16
JAKT_GIR_VARSEL = True    # Send e-post når en ny god jaktdag dukker opp

# Egen e-post til hjemmebane når nye jaktdager dukker opp.
# Mottakere legges inn som GitHub-secret MAIL_TO_HJEMME.
HJEMME_AKTIV = True
JAKTLAGET = "Jaktlaget"   # Underskrift, f.eks. "Even og Norman"

# MET krever at du identifiserer deg med navn på appen og kontaktinfo.
KONTAKT = os.environ.get("MET_KONTAKT", "snovarsel/1.0 ukjent@example.com")

TILSTANDSFIL = Path(__file__).with_name("varslet.json")
TIDSSONE = ZoneInfo("Europe/Oslo")
API = "https://api.met.no/weatherapi/locationforecast/2.0/complete"

UKEDAGER = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
UKEDAGER_LANG = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
KOMPASS = ["N", "NØ", "Ø", "SØ", "S", "SV", "V", "NV"]
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


def analyser_dager(prognose):
    """Lager et sammendrag per dag:
    {dato: {"mm", "min_temp", "vind", "kast", "retning"}}.
    "mm" er snømengde (0 hvis ingen snø), "min_temp" laveste temperatur
    mens det snør, og vind er sterkeste middelvind med kast og retning."""
    grense = datetime.now(timezone.utc) + timedelta(days=DAGER_FREMOVER)
    dager = {}
    for punkt in prognose["properties"]["timeseries"]:
        tid = datetime.fromisoformat(punkt["time"].replace("Z", "+00:00"))
        if tid > grense:
            break
        data = punkt["data"]
        detaljer = data["instant"]["details"]
        dato = tid.astimezone(TIDSSONE).date().isoformat()
        dag = dager.setdefault(dato, {
            "mm": 0.0, "min_temp": None, "vind": None, "kast": None, "retning": None,
            "kast_dag": None,
        })

        # Vind
        vind = detaljer.get("wind_speed")
        if vind is not None and (dag["vind"] is None or vind > dag["vind"]):
            dag["vind"] = vind
            dag["retning"] = detaljer.get("wind_from_direction")
        kast = detaljer.get("wind_speed_of_gust")
        if kast is not None and (dag["kast"] is None or kast > dag["kast"]):
            dag["kast"] = kast

        # Sterkeste kast på dagtid (for jaktforhold). Mangler kast, brukes middelvind.
        if JAKT_FRA_KL <= tid.astimezone(TIDSSONE).hour <= JAKT_TIL_KL:
            dagkast = kast if kast is not None else vind
            if dagkast is not None and (dag["kast_dag"] is None or dagkast > dag["kast_dag"]):
                dag["kast_dag"] = dagkast

        # Snø – bruk timesprognose der den finnes, ellers 6-timersprognose
        periode = data.get("next_1_hours") or data.get("next_6_hours")
        if not periode:
            continue
        symbol = periode["summary"]["symbol_code"]
        mm = periode.get("details", {}).get("precipitation_amount", 0.0)
        if not er_sno(symbol) or mm < MIN_NEDBOR_MM:
            continue
        dag["mm"] += mm
        temp = detaljer.get("air_temperature")
        if temp is not None and (dag["min_temp"] is None or temp < dag["min_temp"]):
            dag["min_temp"] = temp
    return dager


def marker_jaktdager(dager):
    """Setter info["jakt"] = True på dager med gode jaktforhold, og
    info["jakt_sno"] = snømengden de foregående dagene."""
    for dato_str, info in dager.items():
        d = date.fromisoformat(dato_str)
        sno = sum(
            dager.get((d - timedelta(days=k)).isoformat(), {}).get("mm", 0.0)
            for k in range(1, JAKT_DAGER_ETTER_SNO + 1)
        )
        info["jakt_sno"] = sno
        info["jakt"] = (
            sno >= JAKT_MIN_SNO_MM
            and info.get("kast_dag") is not None
            and info["kast_dag"] <= JAKT_MAKS_KAST
        )


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


def kompass(grader):
    if grader is None:
        return ""
    return KOMPASS[int((grader + 22.5) % 360 // 45)]


def fmt_vind(info):
    if info.get("vind") is None:
        return "–"
    tekst = f"{info['vind']:.0f} m/s"
    if info.get("kast") is not None:
        tekst += f" (kast {info['kast']:.0f})"
    if info.get("retning") is not None:
        tekst += f" fra {kompass(info['retning'])}"
    return tekst


def vind_farge(ms):
    """Bakgrunns- og tekstfarge etter vindstyrke (Yr sine kategorier)."""
    if ms < 5.5:    # flau vind / lett bris
        return "#f6f8fa", "#5b6b7a"
    if ms < 10.8:   # bris
        return "#fff3cd", "#6b5300"
    if ms < 17.2:   # kuling
        return "#ffd29a", "#7a3d00"
    return "#e8743b", "#ffffff"   # storm


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
                f"ned mot {fmt_temp(v['info']['min_temp'])}, "
                f"vind {fmt_vind(v['info'])}"
            )
            if v["var_tidlig"]:
                linje += " (varslet tidlig, står fortsatt i prognosen)"
            linjer.append(linje)
        if linjer:
            deler.append(tittel + ":\n" + "\n".join(linjer))
    jakt = [
        f"• {v['sted']['navn']}: {lang_dato(v['dato'])} – "
        f"{v['info']['jakt_sno']:.1f} mm snø dagene før, "
        f"kast opptil {v['info']['kast_dag']:.0f} m/s på dagtid"
        for v in sorted((v for v in nye if v["type"] == "jakt"), key=lambda v: v["dato"])
    ]
    if jakt:
        deler.append("GODE JAKTFORHOLD – nysnø og rolig vind:\n" + "\n".join(jakt))
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
            if info is None or info["mm"] <= 0:
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


def lag_vind_html(oversikt, i_dag):
    """Rutenett med sterkeste vind per dag for alle steder."""
    datoer = [i_dag + timedelta(days=i) for i in range(DAGER_FREMOVER + 1)]

    hode = (
        f'<th style="padding:8px 10px;text-align:left;font-size:12px;color:#5b6b7a;'
        f'border-bottom:2px solid #d5e2ee;">Sted</th>'
    )
    for i, d in enumerate(datoer):
        bg = "#f3f8fd" if i <= BEKREFTET_DAGER else "#ffffff"
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
            f'border-bottom:1px solid #e6edf3;white-space:nowrap;">{html.escape(navn)}</td>'
        )
        for d in datoer:
            info = oversikt.get(navn, {}).get(d.isoformat())
            if not info or info.get("vind") is None:
                rad += (
                    '<td style="padding:6px 4px;text-align:center;color:#c2ccd6;'
                    'border-bottom:1px solid #e6edf3;">·</td>'
                )
                continue
            bg, fg = vind_farge(info["vind"])
            under = []
            if info.get("kast") is not None:
                under.append(f"{info['kast']:.0f}")
            if info.get("retning") is not None:
                under.append(kompass(info["retning"]))
            rad += (
                f'<td style="padding:5px 3px;text-align:center;background:{bg};color:{fg};'
                f'border-bottom:1px solid #ffffff;border-left:1px solid #ffffff;">'
                f'<div style="font-size:13px;font-weight:700;">{info["vind"]:.0f}</div>'
                f'<div style="font-size:10px;white-space:nowrap;">{" · ".join(under)}</div></td>'
            )
        rader += f"<tr>{rad}</tr>"

    def merke(farge, tekst, bg):
        return (
            f'<span style="display:inline-block;background:{bg};color:{farge};'
            f'padding:1px 6px;border-radius:3px;margin-right:4px;">{tekst}</span>'
        )

    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        f'style="border-collapse:collapse;font-family:{FONT};">'
        f"<tr>{hode}</tr>{rader}</table>"
        f'<p style="margin:10px 0 0;font-size:12px;color:#5b6b7a;line-height:1.8;">'
        f"Stort tall = sterkeste middelvind (m/s). Under: vindkast · retning vinden kommer fra.<br>"
        f'{merke("#5b6b7a", "under 5.5 svak", "#f6f8fa")}'
        f'{merke("#6b5300", "5.5–10.7 bris", "#fff3cd")}'
        f'{merke("#7a3d00", "10.8–17.1 kuling", "#ffd29a")}'
        f'{merke("#ffffff", "17.2+ storm", "#e8743b")}</p>'
    )


def lag_jakt_html(oversikt, nye_nokler, i_dag):
    """Rutenett som viser hvilke dager som har gode jaktforhold."""
    datoer = [i_dag + timedelta(days=i) for i in range(DAGER_FREMOVER + 1)]
    hode = (
        f'<th style="padding:8px 10px;text-align:left;font-size:12px;color:#5b6b7a;'
        f'border-bottom:2px solid #d5e2ee;">Sted</th>'
    )
    for i, d in enumerate(datoer):
        bg = "#f3f8fd" if i <= BEKREFTET_DAGER else "#ffffff"
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
            f'border-bottom:1px solid #e6edf3;white-space:nowrap;">{html.escape(navn)}</td>'
        )
        for d in datoer:
            info = oversikt.get(navn, {}).get(d.isoformat())
            if not info or not info.get("jakt"):
                rad += (
                    '<td style="padding:6px 4px;text-align:center;color:#c2ccd6;'
                    'border-bottom:1px solid #e6edf3;">·</td>'
                )
                continue
            ny = (navn, d.isoformat()) in nye_nokler
            ramme = "border:2px solid #e0712c;" if ny else "border-bottom:1px solid #ffffff;"
            rad += (
                f'<td style="padding:5px 3px;text-align:center;background:#cdebd3;'
                f'color:#1b5e2b;{ramme}">'
                f'<div style="font-size:14px;font-weight:700;">✓</div>'
                f'<div style="font-size:10px;white-space:nowrap;">{info["kast_dag"]:.0f} m/s</div></td>'
            )
        rader += f"<tr>{rad}</tr>"
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        f'style="border-collapse:collapse;font-family:{FONT};">'
        f"<tr>{hode}</tr>{rader}</table>"
        f'<p style="margin:10px 0 0;font-size:12px;color:#5b6b7a;line-height:1.6;">'
        f"✓ = minst {JAKT_MIN_SNO_MM:g} mm snø de siste {JAKT_DAGER_ETTER_SNO} dagene før, "
        f"og vindkast på høyst {JAKT_MAKS_KAST:g} m/s mellom kl. {JAKT_FRA_KL:02d} og "
        f"{JAKT_TIL_KL:02d}. Tallet er kraftigste kast på dagtid. "
        f"Oransje ramme = ny jaktdag i denne e-posten.</p>"
    )


def lag_jakt_detaljer_html(nye):
    rader = sorted((v for v in nye if v["type"] == "jakt"), key=lambda v: (v["dato"], v["sted"]["navn"]))
    if not rader:
        return ""
    cell = "padding:8px 10px;font-size:14px;border-bottom:1px solid #e6edf3;color:#1f2d3a;"
    th = (
        "padding:8px 10px;font-size:12px;text-align:left;color:#5b6b7a;"
        "border-bottom:2px solid #d5e2ee;"
    )
    linjer = "".join(
        f"<tr>"
        f'<td style="{cell}font-weight:600;">{html.escape(v["sted"]["navn"])}</td>'
        f'<td style="{cell}white-space:nowrap;">{lang_dato(v["dato"])}</td>'
        f'<td style="{cell}white-space:nowrap;">{v["info"]["jakt_sno"]:.1f} mm</td>'
        f'<td style="{cell}white-space:nowrap;">{v["info"]["kast_dag"]:.0f} m/s</td>'
        f'<td style="{cell}white-space:nowrap;"><a href="{yr_lenke(v["sted"])}" '
        f'style="color:#2f6aa3;text-decoration:none;font-weight:600;">Yr →</a></td>'
        f"</tr>"
        for v in rader
    )
    return (
        f'<h2 style="margin:28px 0 4px;font-size:17px;color:#1b5e2b;font-family:{FONT};">'
        f"🌲 Nye gode jaktdager</h2>"
        f'<p style="margin:0 0 10px;font-size:13px;color:#5b6b7a;font-family:{FONT};">'
        f"Nysnø og rolig vind – gode forhold for toppjakt etter tiur og orrfugl.</p>"
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        f'style="border-collapse:collapse;font-family:{FONT};">'
        f'<tr><th style="{th}">Sted</th><th style="{th}">Dag</th>'
        f'<th style="{th}">Snø før</th><th style="{th}">Kast dagtid</th><th style="{th}"></th></tr>'
        f"{linjer}</table>"
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
            f'<td style="{cell}white-space:nowrap;">{fmt_vind(info)}</td>'
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
        f'<th style="{th}">Laveste</th><th style="{th}">Vind</th><th style="{th}"></th></tr>'
        f"{linjer}</table>"
    )


def lag_html(oversikt, nye, i_dag):
    sno = [v for v in nye if v["type"] in ("bekreftet", "tidlig")]
    antall_steder = len({v["sted"]["navn"] for v in sno})
    antall_bekreftet = sum(1 for v in sno if v["type"] == "bekreftet")
    antall_tidlig = len(sno) - antall_bekreftet
    antall_jakt = sum(1 for v in nye if v["type"] == "jakt")

    deler = []
    if sno:
        stedtekst = "1 sted" if antall_steder == 1 else f"{antall_steder} steder"
        typer = []
        if antall_bekreftet:
            typer.append(f"{antall_bekreftet} bekreftet")
        if antall_tidlig:
            typer.append(f"{antall_tidlig} tidlig{'e' if antall_tidlig > 1 else ''}")
        deler.append(f"Ny snø for {stedtekst} ({' og '.join(typer)})")
    if antall_jakt:
        deler.append(f"{antall_jakt} ny{'e' if antall_jakt > 1 else ''} god{'e' if antall_jakt > 1 else ''} jaktdag{'er' if antall_jakt > 1 else ''}")
    sammendrag = " · ".join(deler)

    nye_nokler = {(v["sted"]["navn"], v["dato"].isoformat()) for v in sno}
    nye_jakt = {(v["sted"]["navn"], v["dato"].isoformat()) for v in nye if v["type"] == "jakt"}
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
    {sammendrag}
  </div>
</td></tr>

<tr><td style="padding:24px 28px 8px;">
  <h2 style="margin:0 0 12px;font-size:17px;color:#1f2d3a;">❄️ Snø neste {DAGER_FREMOVER} dager</h2>
  <div style="overflow-x:auto;">{lag_oversikt_html(oversikt, nye_nokler, i_dag)}</div>
  <h2 style="margin:28px 0 12px;font-size:17px;color:#1f2d3a;">💨 Vind neste {DAGER_FREMOVER} dager</h2>
  <div style="overflow-x:auto;">{lag_vind_html(oversikt, i_dag)}</div>
  <h2 style="margin:28px 0 12px;font-size:17px;color:#1f2d3a;">🌲 Jaktforhold neste {DAGER_FREMOVER} dager</h2>
  <div style="overflow-x:auto;">{lag_jakt_html(oversikt, nye_jakt, i_dag)}</div>
  {lag_jakt_detaljer_html(nye)}
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


def send_epost(emne, tekst, html_innhold=None, mottaker=None, svar_til=None,
               avsender_navn="Snøvarsel"):
    bruker = os.environ["SMTP_USER"]
    passord = os.environ["SMTP_PASS"]
    mottaker = mottaker or os.environ.get("MAIL_TO", bruker)
    vert = os.environ.get("SMTP_HOST", "smtp.gmail.com")

    melding = EmailMessage()
    melding["Subject"] = emne
    melding["From"] = f"{avsender_navn} <{bruker}>"
    melding["To"] = mottaker
    if svar_til:
        melding["Reply-To"] = svar_til
    melding.set_content(tekst)
    if html_innhold:
        melding.add_alternative(html_innhold, subtype="html")

    with smtplib.SMTP_SSL(vert, 465) as server:
        server.login(bruker, passord)
        server.send_message(melding)


# ---------------------------------------------------------------------------
# E-post til hjemmebane («Jaktkomiteen informerer»)
# ---------------------------------------------------------------------------

SIKKERHET = {
    3: ("Ganske sikkert", "#2e7d32"),
    2: ("Godt mulig", "#e8701a"),
    1: ("Tidlig antydning", "#9e9e9e"),
}


def sikkerhet(dato, i_dag):
    unna = (dato - i_dag).days
    if unna <= BEKREFTET_DAGER:
        return 3
    if unna <= 5:
        return 2
    return 1


def samle_jaktdager(oversikt, i_dag):
    """Alle gode jaktdager fremover, gruppert per dato:
    [(dato, [(stedsnavn, info), ...]), ...] sortert etter dato."""
    per_dato = {}
    for sted in STEDER:
        for dato_str, info in oversikt.get(sted["navn"], {}).items():
            dato = date.fromisoformat(dato_str)
            if info.get("jakt") and dato >= i_dag:
                per_dato.setdefault(dato, []).append((sted["navn"], info))
    return sorted(per_dato.items())


def velg_favoritt(jaktdager, i_dag):
    """Den mest sannsynlige dagen: sikrest prognose, deretter mest snø og minst vind."""
    if not jaktdager:
        return None
    def poeng(element):
        dato, steder = element
        sno = max(info["jakt_sno"] for _, info in steder)
        kast = min(info["kast_dag"] for _, info in steder)
        return (sikkerhet(dato, i_dag), sno - kast, -dato.toordinal())
    return max(jaktdager, key=poeng)[0]


def maaler_html(niva):
    farge = SIKKERHET[niva][1]
    celler = "".join(
        f'<td style="width:22px;height:10px;background:{farge if i < niva else "#e3ddd0"};'
        f'border-radius:5px;"></td><td style="width:4px;"></td>'
        for i in range(3)
    )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="display:inline-table;'
        f'vertical-align:middle;"><tr>{celler}</tr></table>'
    )


def lag_hjemme_emne(jaktdager, favoritt):
    fav = lang_dato(favoritt)
    andre = len(jaktdager) - 1
    tillegg = f" (+{andre} dag{'er' if andre > 1 else ''} til)" if andre > 0 else ""
    return f"📣 Jaktkomiteen informerer: Mulig jakt {fav}{tillegg}"


def lag_hjemme_tekst(jaktdager, favoritt, i_dag):
    linjer = [
        "Kjære ledelse på hjemmebane!",
        "",
        "Værgudene har talt: Det er meldt nysnø og vindstille – akkurat det tiuren frykter mest.",
        "Vi melder derfor fra i god tid, slik dere har bedt om. Følgende dager er aktuelle:",
        "",
    ]
    for dato, steder in jaktdager:
        niva = SIKKERHET[sikkerhet(dato, i_dag)][0]
        fav = "  ⭐ FAVORITT" if dato == favoritt else ""
        linjer.append(f"• {lang_dato(dato)}: {', '.join(n for n, _ in steder)} – {niva}{fav}")
    linjer += [
        "",
        "Forventet fravær: fra morgengry til mørket faller på.",
        "Forventet fangst: uviss (som vanlig).",
        "Værmeldinger kan endre seg – ny beskjed kommer så snart vi vet mer.",
        "",
        f"Med ærbødig hilsen\n{JAKTLAGET}",
    ]
    return "\n".join(linjer)


def lag_hjemme_html(jaktdager, nye_datoer, favoritt, i_dag, svar_til):
    font = "Georgia,'Times New Roman',serif"
    sans = FONT
    svar = quote(svar_til.replace(" ", ""), safe="@,")

    kort = ""
    for dato, steder in jaktdager:
        niva = sikkerhet(dato, i_dag)
        tekst_niva, farge_niva = SIKKERHET[niva]
        er_fav = dato == favoritt
        ramme = "3px solid #e8701a" if er_fav else "1px solid #e3ddd0"
        merker = ""
        if er_fav:
            merker += (
                '<span style="display:inline-block;background:#e8701a;color:#ffffff;font-size:11px;'
                'font-weight:700;padding:3px 8px;border-radius:10px;margin-right:6px;'
                'letter-spacing:0.5px;">⭐ JAKTKOMITEENS FAVORITT</span>'
            )
        if dato in nye_datoer:
            merker += (
                '<span style="display:inline-block;background:#1f4d2b;color:#ffffff;font-size:11px;'
                'font-weight:700;padding:3px 8px;border-radius:10px;letter-spacing:0.5px;">NY</span>'
            )
        stedliste = "<br>".join(
            f'📍 <b>{html.escape(navn)}</b> '
            f'<span style="color:#7a6f5c;font-size:13px;">– {info["jakt_sno"]:.0f} cm nysnø, '
            f'vind {info["kast_dag"]:.0f} m/s</span>'
            for navn, info in steder
        )
        kort += f"""
<tr><td style="padding:0 0 14px;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#ffffff;border:{ramme};border-radius:12px;">
<tr>
  <td width="92" valign="top" style="padding:16px 0 16px 16px;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="76"
           style="border-radius:8px;overflow:hidden;border:1px solid #d9cfbb;">
      <tr><td style="background:#c0392b;color:#ffffff;text-align:center;font-family:{sans};
                     font-size:12px;font-weight:700;padding:4px 0;text-transform:uppercase;
                     letter-spacing:1px;">{UKEDAGER[dato.weekday()]}</td></tr>
      <tr><td style="background:#ffffff;text-align:center;font-family:{font};font-size:34px;
                     font-weight:700;color:#2b2b2b;padding:4px 0 0;line-height:1;">{dato.day}</td></tr>
      <tr><td style="background:#ffffff;text-align:center;font-family:{sans};font-size:12px;
                     color:#7a6f5c;padding:2px 0 6px;">{MANEDER[dato.month - 1]}</td></tr>
    </table>
  </td>
  <td valign="top" style="padding:16px 18px 16px 14px;font-family:{sans};color:#2b2b2b;">
    <div style="margin-bottom:6px;">{merker}</div>
    <div style="font-family:{font};font-size:19px;font-weight:700;margin-bottom:6px;">
      {UKEDAGER_LANG[dato.weekday()].capitalize()} {dato.day}. {MANEDER[dato.month - 1]}
    </div>
    <div style="font-size:14px;line-height:1.7;margin-bottom:10px;">{stedliste}</div>
    <div style="font-size:13px;color:#7a6f5c;">
      Sannsynlighet: {maaler_html(niva)}
      <b style="color:{farge_niva};margin-left:6px;">{tekst_niva}</b>
    </div>
  </td>
</tr></table>
</td></tr>"""

    def knapp(tekst, emne, bg, fg):
        return (
            f'<a href="mailto:{svar}?subject={quote(emne)}" style="display:inline-block;'
            f'background:{bg};color:{fg};font-family:{sans};font-size:14px;font-weight:700;'
            f'text-decoration:none;padding:12px 18px;border-radius:24px;margin:4px;">{tekst}</a>'
        )

    fav_tekst = lang_dato(favoritt)
    return f"""\
<!DOCTYPE html>
<html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
</head>
<body style="margin:0;padding:0;background:#efe8da;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#efe8da;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="max-width:600px;background:#f8f4ec;border-radius:16px;overflow:hidden;">

<tr><td style="background:#1f4d2b;padding:30px 28px 26px;text-align:center;">
  <div style="font-size:44px;line-height:1;">🌲📣🌲</div>
  <div style="font-family:{sans};font-size:12px;color:#f3c58f;letter-spacing:3px;
              font-weight:700;margin-top:14px;">OFFISIELT VARSEL</div>
  <div style="font-family:{font};font-size:30px;font-weight:700;color:#ffffff;margin-top:6px;">
    Jaktkomiteen informerer</div>
  <div style="font-family:{sans};font-size:14px;color:#cfe3d3;margin-top:8px;">
    Til ledelsen på hjemmebane – til orientering og (forhåpentlig) godkjenning</div>
</td></tr>

<tr><td style="background:#e8701a;padding:10px 28px;text-align:center;font-family:{sans};
               font-size:14px;font-weight:700;color:#ffffff;">
  ⭐ Mest sannsynlige jaktdag: {fav_tekst}
</td></tr>

<tr><td style="padding:26px 28px 6px;font-family:{font};font-size:16px;line-height:1.6;color:#2b2b2b;">
  <p style="margin:0 0 12px;">Kjære ledelse,</p>
  <p style="margin:0 0 12px;">Værgudene har talt. Det er meldt <b>nysnø</b> og <b>vindstille</b>
  – den farligste kombinasjonen som finnes for en tiur. Jaktlaget har derfor
  den store ære å melde fra i god tid, nøyaktig slik dere har bedt om
  (gjentatte ganger).</p>
  <p style="margin:0 0 18px;">Følgende dager er under vurdering:</p>
</td></tr>

<tr><td style="padding:0 28px;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%">{kort}</table>
</td></tr>

<tr><td style="padding:10px 28px 4px;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#fff8ec;border:2px dashed #d9b98a;border-radius:12px;">
<tr><td style="padding:18px 20px;font-family:{sans};font-size:14px;line-height:1.9;color:#2b2b2b;">
  <div style="font-family:{font};font-size:17px;font-weight:700;margin-bottom:6px;">
    📋 Praktisk informasjon</div>
  🕔 <b>Forventet fravær:</b> fra morgengry til mørket faller på<br>
  🪶 <b>Forventet fangst:</b> uviss (som vanlig)<br>
  📵 <b>Dekning:</b> varierende – vi ringer når vi er tilbake på veien<br>
  🍽️ <b>Middag:</b> jaktlaget tar oppvasken dagen etter (dette er et løfte)
</td></tr></table>
</td></tr>

<tr><td style="padding:22px 28px 8px;text-align:center;">
  <div style="font-family:{font};font-size:17px;font-weight:700;color:#2b2b2b;margin-bottom:10px;">
    Ledelsens vurdering:</div>
  {knapp("👍 Godkjent!", "Godkjent – god jakt!", "#2e7d32", "#ffffff")}
  {knapp("🤔 Vi må snakke sammen", "Om jaktplanene …", "#ffffff", "#1f4d2b")}
</td></tr>

<tr><td style="padding:18px 28px 26px;text-align:center;font-family:{sans};font-size:12px;
               line-height:1.6;color:#8a7f6a;">
  Med ærbødig hilsen<br>
  <span style="font-family:{font};font-size:16px;color:#2b2b2b;font-style:italic;">{html.escape(JAKTLAGET)}</span>
  <br><br>
  Jaktkomiteen tar forbehold om at værmeldingen kan endre seg.
  Ny beskjed kommer så snart vi vet mer.<br>
  Grønn måler = ganske sikkert · oransje = godt mulig · grå = tidlig antydning.
</td></tr>

</table></td></tr></table></body></html>"""


def send_hjemme_epost(oversikt, nye, i_dag, mottaker=None, test=False):
    """Sender «Jaktkomiteen informerer» når det har dukket opp nye jaktdager."""
    mottaker = mottaker or os.environ.get("MAIL_TO_HJEMME", "").strip()
    if not (HJEMME_AKTIV and mottaker):
        return
    nye_datoer = {v["dato"] for v in nye if v["type"] == "jakt"}
    if not nye_datoer:
        return
    jaktdager = samle_jaktdager(oversikt, i_dag)
    favoritt = velg_favoritt(jaktdager, i_dag)
    if favoritt is None:
        return
    svar_til = os.environ.get("MAIL_TO") or os.environ.get("SMTP_USER", "")
    emne = lag_hjemme_emne(jaktdager, favoritt)
    if test:
        emne = "✅ TEST – " + emne
    send_epost(
        emne,
        lag_hjemme_tekst(jaktdager, favoritt, i_dag),
        lag_hjemme_html(jaktdager, nye_datoer, favoritt, i_dag, svar_til),
        mottaker=mottaker,
        svar_til=svar_til,
        avsender_navn="Jaktkomiteen",
    )
    print(f"Hjemmebane-e-post sendt til {mottaker}.")


# ---------------------------------------------------------------------------
# Hovedprogram
# ---------------------------------------------------------------------------


def lag_emne(nye):
    typer = {v["type"] for v in nye}
    deler = []
    if "bekreftet" in typer:
        deler.append("❄️ Snø bekreftet!")
    elif "tidlig" in typer:
        deler.append("🌨️ Tidlig snøvarsel")
    if "jakt" in typer:
        deler.append("🌲 Gode jaktforhold")
    return " · ".join(deler)


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

        dager = analyser_dager(prognose)
        marker_jaktdager(dager)
        oversikt[sted["navn"]] = dager
        snodager = {d: info for d, info in dager.items() if info["mm"] > 0}

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

        for dato_str, info in sorted(dager.items()):
            if not (JAKT_GIR_VARSEL and info.get("jakt")):
                continue
            nokkel = f"{sted['navn']}|{dato_str}|jakt"
            if nokkel in tilstand:
                continue
            tilstand[nokkel] = naa
            nye.append({"sted": sted, "dato": date.fromisoformat(dato_str), "info": info,
                        "type": "jakt", "var_tidlig": False})

    if nye:
        emne = lag_emne(nye)
        tekst = lag_tekst(nye)
        print(tekst)
        send_epost(emne, tekst, lag_html(oversikt, nye, i_dag))
        send_hjemme_epost(oversikt, nye, i_dag)
    else:
        print("Ingen ny snø eller nye jaktdager i prognosen.")

    lagre_tilstand(tilstand)


def send_test():
    """Sender en eksempel-e-post med oppdiktet snø, så du ser hvordan den ser ut."""
    i_dag = datetime.now(TIDSSONE).date()
    eksempel = [(0, 1, 2.4, -3), (0, 4, 6.1, -6), (1, 2, 0.6, -1), (2, 5, 3.0, -4),
                (3, 1, 8.5, -7), (3, 2, 12.3, -9), (4, 0, 4.2, -11), (4, 3, 15.0, -14)]
    oversikt, nye = {}, []
    retninger = [270, 225, 315, 180, 0, 45, 90, 135]
    for s_idx, sted in enumerate(STEDER):
        for dag in range(DAGER_FREMOVER + 1):
            vind = 2 + (s_idx * 3 + dag * 2) % 16 + (6 if sted["navn"] == "Galdhøpiggen" else 0)
            oversikt.setdefault(sted["navn"], {})[(i_dag + timedelta(days=dag)).isoformat()] = {
                "mm": 0.0, "min_temp": None, "vind": float(vind),
                "kast": float(vind * 1.6), "retning": retninger[(s_idx + dag) % 8],
                "kast_dag": float(vind * 1.4),
            }
    # Noen rolige dager etter snøfall, så eksempelet viser gode jaktdager
    for s_idx, dag in [(0, 2), (0, 5), (1, 3), (2, 6), (3, 3), (3, 4)]:
        if s_idx < len(STEDER):
            info = oversikt[STEDER[s_idx]["navn"]][(i_dag + timedelta(days=dag)).isoformat()]
            info.update(vind=2.0, kast=4.0, kast_dag=4.0)
    for s_idx, dag, mm, temp in eksempel:
        if s_idx >= len(STEDER):
            continue
        sted = STEDER[s_idx]
        dato = i_dag + timedelta(days=dag)
        info = oversikt[sted["navn"]][dato.isoformat()]
        info.update(mm=mm, min_temp=temp)
        nye.append({
            "sted": sted, "dato": dato, "info": info,
            "type": "bekreftet" if dag <= BEKREFTET_DAGER else "tidlig",
            "var_tidlig": dag == 1,
        })
    for sted in STEDER:
        marker_jaktdager(oversikt[sted["navn"]])
        for dato_str, info in sorted(oversikt[sted["navn"]].items()):
            if info["jakt"]:
                nye.append({"sted": sted, "dato": date.fromisoformat(dato_str), "info": info,
                            "type": "jakt", "var_tidlig": False})
    send_epost(
        "✅ Test fra Snøvarsel (eksempeldata)",
        "TEST – dette er oppdiktede tall, ikke en ekte prognose.\n\n" + lag_tekst(nye),
        lag_html(oversikt, nye, i_dag).replace(
            "❄️ Snøvarsel</div>",
            "❄️ Snøvarsel <span style=\"font-size:13px;background:#e0712c;"
            "padding:3px 8px;border-radius:4px;vertical-align:middle;\">TEST</span></div>",
        ),
    )
    # Hjemmebane-e-posten sendes som test til jegerne selv, ikke til hjemmebane
    send_hjemme_epost(oversikt, nye, i_dag,
                      mottaker=os.environ.get("MAIL_TO") or os.environ["SMTP_USER"], test=True)
    print("Test-e-post sendt.")


if __name__ == "__main__":
    if os.environ.get("TEST_EPOST") == "true":
        send_test()
    else:
        main()
