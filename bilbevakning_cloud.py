#!/usr/bin/env python3
"""
Bilbevakning – hittar undervärderade bilannonser på Blocket.

Vad gör skriptet?
1. Söker igenom Blocket (via paketet blocket-api, som pratar direkt med
   Blockets eget sök-API för bilar – ingen inloggning krävs).
2. Grupperar träffarna på märke + modell + ungefärligt årsspann.
3. Jämför varje annons pris mot medianpriset i sin grupp. Om priset är
   X % lägre än medianen (se config.json) flaggas den som ett möjligt kap.
4. Mailar dig en sammanfattning, och kommer inte mejla samma annons igen.

OBS om "car.info-jämförelse":
De flesta säljare döljer registreringsnumret i annonstexten (av
integritetsskäl), så en automatisk regnr -> car.info-koppling skulle bara
träffa ett fåtal annonser och bygger dessutom på en oofficiell, odokumenterad
sökväg på car.info som lätt går sönder. Skriptet bygger därför
"undervärderad"-bedömningen på en statistisk jämförelse mot andra Blocket-
annonser istället – det är robust och kräver inget regnr. Varje flaggad
annons får en länk så du snabbt kan slå upp bilen manuellt (t.ex. på
car.info) om säljaren råkat ange regnr i texten, innan du hör av dig.

Kör:
    python3 bilbevakning.py            # kör i en loop, en gång per timme (se config.json)
    python3 bilbevakning.py --once     # kör en enda sökning och avsluta
    python3 bilbevakning.py --debug-raw  # sparar rådata från Blocket till debug_raw.json
                                          # (bra om parsningen inte hittar några annonser
                                          # och du vill se hur svaret faktiskt ser ut)
"""

from __future__ import annotations

import argparse
import json
import re
import smtplib
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from blocket_api import BlocketAPI, CarAd, CarModel, Location

HAR = Path(__file__).resolve().parent
CONFIG_PATH = HAR / "config.json"
STATE_PATH = HAR / "sedda_annonser.json"
DEBUG_RAW_PATH = HAR / "debug_raw.json"


# --------------------------------------------------------------------------
# Datamodell för en normaliserad annons
# --------------------------------------------------------------------------
@dataclass
class Annons:
    id: str
    titel: str
    pris: int
    marke: str | None
    modell: str | None
    ar: int | None
    miltal: int | None
    plats: str | None
    url: str
    regnr: str | None = None
    drivmedel: str | None = None
    ovrig_text: str = ""


# --------------------------------------------------------------------------
# Konfiguration & state (vilka annonser vi redan mejlat om)
# --------------------------------------------------------------------------
def las_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Hittar ingen config.json i {HAR}. Kopiera/döp om exempelfilen först.")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def las_state() -> set[str]:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def spara_state(sedda: set[str]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(sedda), f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Hämta annonser från Blocket
# --------------------------------------------------------------------------
def bygg_plats_lista(namn: list[str]) -> list[Location]:
    platser = []
    for n in namn:
        try:
            platser.append(Location[n.upper()])
        except KeyError:
            print(f"⚠️  Okänd region i config.json: '{n}' – hoppar över den.")
    return platser


def bygg_marke_lista(namn: list[str]) -> list[CarModel]:
    marken = []
    for n in namn:
        key = n.upper().replace(" ", "_").replace("-", "_")
        try:
            marken.append(CarModel[key])
        except KeyError:
            print(f"⚠️  Okänt märke i config.json: '{n}' – hoppar över det. "
                  f"Kolla exakt skrivning i README.")
    return marken


def _forsta_varde(d: dict, *nycklar: str) -> Any:
    """Testar flera möjliga nyckelnamn (API-svar från Blocket har skiftat form över tid)."""
    for nyckel in nycklar:
        if nyckel in d and d[nyckel] not in (None, ""):
            return d[nyckel]
    return None


def _parametervarde(parametrar: list[dict], *etiketter: str) -> Any:
    """
    Letar i en Blocket-style 'parameters': [{'label':..., 'value':...}] lista.
    Testar exakt matchning först (så t.ex. 'Modell' inte råkar matcha 'Modellår'),
    och faller tillbaka på delsträngsmatchning om inget exakt hittas.
    """
    if not isinstance(parametrar, list):
        return None
    etiketter_lower = [e.lower() for e in etiketter]

    for p in parametrar:
        if isinstance(p, dict) and str(p.get("label", "")).strip().lower() in etiketter_lower:
            return p.get("value")

    for p in parametrar:
        if not isinstance(p, dict):
            continue
        label = str(p.get("label", "")).lower()
        if any(e in label for e in etiketter_lower):
            return p.get("value")
    return None


def _till_int(varde: Any) -> int | None:
    if varde is None:
        return None
    if isinstance(varde, (int, float)):
        return int(varde)
    siffror = "".join(c for c in str(varde) if c.isdigit())
    return int(siffror) if siffror else None


def normalisera_annons(rad: dict) -> Annons | None:
    """
    Plockar ut fälten vi bryr oss om ur ett Blocket-annons-objekt.
    Bekräftad verklig struktur (från debug_raw.json, sept 2026):
    {"ad_id": 123, "heading": "...", "location": "Västerås",
     "price": {"amount": 45000, ...}, "year": 2012, "mileage": 22300,
     "make": "...", "model": "...", "regno": "...", "canonical_url": "..."}
    Vi testar ändå flera möjliga nyckelnamn som fallback ifall Blocket
    ändrar strukturen igen i framtiden.
    """
    d = rad.get("ad", rad) if isinstance(rad, dict) else {}

    ad_id = _forsta_varde(d, "ad_id", "id", "list_id")
    if ad_id is None:
        return None
    ad_id = str(ad_id)

    titel = _forsta_varde(d, "heading", "facade_title", "subject", "title") or "(okänd titel)"

    pris_rad = _forsta_varde(d, "price")
    if isinstance(pris_rad, dict):
        pris = _till_int(pris_rad.get("amount", pris_rad.get("value")))
    else:
        pris = _till_int(pris_rad)
    if pris is None:
        return None

    parametrar = d.get("parameters", [])

    marke = _forsta_varde(d, "make", "brand") or _parametervarde(parametrar, "märke")
    modell = _forsta_varde(d, "model") or _parametervarde(parametrar, "modell")

    ar = _till_int(_forsta_varde(d, "year", "model_year") or _parametervarde(parametrar, "modellår", "år"))
    miltal = _till_int(_forsta_varde(d, "mileage") or _parametervarde(parametrar, "miltal"))

    plats_rad = _forsta_varde(d, "location")
    if isinstance(plats_rad, list) and plats_rad:
        plats = plats_rad[-1].get("name") if isinstance(plats_rad[-1], dict) else str(plats_rad[-1])
    elif isinstance(plats_rad, str):
        plats = plats_rad
    else:
        plats = None

    regnr = _forsta_varde(d, "regno", "regnr")
    drivmedel = _forsta_varde(d, "fuel") or _parametervarde(parametrar, "drivmedel", "bränsle")

    url = (_forsta_varde(d, "canonical_url", "url")
           or f"https://www.blocket.se/mobility/item/{ad_id}")

    ovrig = " ".join(str(x) for x in [
        d.get("body", ""), d.get("description", "")
    ] if x)

    return Annons(
        id=ad_id, titel=str(titel), pris=pris,
        marke=str(marke) if marke else None,
        modell=str(modell) if modell else None,
        ar=ar, miltal=miltal, plats=plats, url=url,
        regnr=str(regnr) if regnr else None,
        drivmedel=str(drivmedel) if drivmedel else None,
        ovrig_text=ovrig,
    )


def hamta_annonser(cfg: dict, spara_raw: bool = False) -> list[Annons]:
    sok = cfg["sokkriterier"]
    korning = cfg["korning"]
    marken = bygg_marke_lista(sok.get("marken", []))

    # Bakåtkompatibelt: om config.json fortfarande har den gamla platta
    # "regioner"-listan istället för "sokningar", gör om den till en enda grupp.
    grupper = sok.get("sokningar")
    if not grupper:
        grupper = [{"regioner": sok.get("regioner", []), "endast_orter": []}]

    alla: dict[str, Annons] = {}  # id -> Annons, för att slå ihop grupper utan dubbletter
    misslyckade_parsningar = 0

    for grupp in grupper:
        platser = bygg_plats_lista(grupp.get("regioner", []))
        endast_orter = [o.lower() for o in grupp.get("endast_orter", [])]

        annonser_i_grupp, fel = _sok_region(
            platser, marken, sok, korning, spara_raw=spara_raw
        )
        misslyckade_parsningar += fel

        for a in annonser_i_grupp:
            if endast_orter:
                plats_lower = (a.plats or "").lower()
                if not any(ort in plats_lower for ort in endast_orter):
                    continue  # inte i någon av de tillåtna orterna, hoppa över
            alla[a.id] = a  # dedupar automatiskt på annons-id

    # Extra säkerhetsfilter: Blockets server-side filter är inte alltid 100%
    # tillförlitligt (vi har sett annonser med orimligt högt miltal slinka
    # igenom trots milage_to-parametern), så vi dubbelkollar allt själva här.
    resultat = []
    for a in alla.values():
        if a.pris is not None:
            if sok.get("pris_fran") is not None and a.pris < sok["pris_fran"]:
                continue
            if sok.get("pris_till") is not None and a.pris > sok["pris_till"]:
                continue
        if a.ar is not None:
            if sok.get("ar_fran") is not None and a.ar < sok["ar_fran"]:
                continue
            if sok.get("ar_till") is not None and a.ar > sok["ar_till"]:
                continue
        if a.miltal is not None and sok.get("max_miltal") is not None:
            if a.miltal > sok["max_miltal"]:
                continue
        resultat.append(a)

    if misslyckade_parsningar:
        print(f"⚠️  Kunde inte tolka {misslyckade_parsningar} annonser (troligen "
              f"annat fältnamn i API-svaret). Kör med --debug-raw för att inspektera.")

    return resultat


def _sok_region(
    platser: list[Location], marken: list[CarModel], sok: dict, korning: dict,
    spara_raw: bool = False,
) -> tuple[list[Annons], int]:
    """Söker igenom alla sidor för EN uppsättning platser. Returnerar (annonser, antal_misslyckade)."""
    api = BlocketAPI()
    annonser: list[Annons] = []
    sida = 1
    max_sidor = korning.get("max_sidor_per_sokning", 15)
    misslyckade = 0

    while sida <= max_sidor:
        try:
            svar = api.search_car(
                page=sida,
                locations=platser,
                models=marken,
                price_from=sok.get("pris_fran"),
                price_to=sok.get("pris_till"),
                year_from=sok.get("ar_fran"),
                year_to=sok.get("ar_till"),
                milage_to=sok.get("max_miltal"),
            )
        except Exception as e:
            print(f"❌ Fel vid sökning (sida {sida}): {e}")
            break

        if spara_raw and sida == 1:
            with open(DEBUG_RAW_PATH, "w", encoding="utf-8") as f:
                json.dump(svar, f, ensure_ascii=False, indent=2)
            print(f"📝 Rådata sparad till {DEBUG_RAW_PATH}")

        # Svaret innehåller annonslistan under "docs" (bekräftat sept 2026),
        # men vi testar äldre kända nycklar också som fallback.
        if isinstance(svar, dict):
            rader = (svar.get("docs") or svar.get("data") or svar.get("ads")
                     or svar.get("hits") or svar.get("items") or [])
        elif isinstance(svar, list):
            rader = svar
        else:
            rader = []

        if not rader:
            break

        for rad in rader:
            annons = normalisera_annons(rad)
            if annons:
                annonser.append(annons)
            else:
                misslyckade += 1

        sida += 1
        time.sleep(1)  # var snäll mot Blockets servrar

    return annonser, misslyckade


# --------------------------------------------------------------------------
# Hitta undervärderade annonser
# --------------------------------------------------------------------------
def gruppnyckel(a: Annons) -> tuple:
    ar_bucket = (a.ar // 3) if a.ar else None
    return ((a.marke or "").lower(), (a.modell or "").lower(), ar_bucket)


def hitta_kap(annonser: list[Annons], cfg: dict) -> list[tuple[Annons, float, int]]:
    """Returnerar lista av (annons, medianpris i gruppen, procent under median)."""
    analys = cfg["analys"]
    min_grupp = analys.get("min_annonser_for_jamforelse", 4)
    troskel = analys.get("undervarderad_procent", 15) / 100

    grupper: dict[tuple, list[Annons]] = {}
    for a in annonser:
        if not a.marke or not a.modell:
            continue
        grupper.setdefault(gruppnyckel(a), []).append(a)

    kap = []
    for grupp, medlemmar in grupper.items():
        if len(medlemmar) < min_grupp:
            continue
        for a in medlemmar:
            ovriga_priser = [m.pris for m in medlemmar if m.id != a.id]
            if len(ovriga_priser) < min_grupp - 1:
                continue
            median = statistics.median(ovriga_priser)
            if median <= 0:
                continue
            andel_under = (median - a.pris) / median
            if andel_under >= troskel:
                kap.append((a, median, round(andel_under * 100)))

    kap.sort(key=lambda x: x[2], reverse=True)
    return kap


# --------------------------------------------------------------------------
# Djupanalys – hämtar hela annonsen EN gång per kandidat och gör en
# regelbaserad genomgång: skadeord, kända motorproblem för märket/modellen,
# och plus/minus utifrån vad som faktiskt står i texten.
#
# OBS: Detta är HEURISTIK (nyckelordsmatchning), inte en riktig besiktning.
# Den kan missa saker eller misstolka text. Använd som ett första sovsåll,
# läs alltid hela annonsen själv innan du hör av dig till säljaren.
# --------------------------------------------------------------------------
STANDARD_SKADEORD = [
    "krockskad", "krockad", "totalskad", "motorhaveri", "motorstopp",
    "startar inte", "startar ej", "går ej att köra", "går inte att köra",
    "ej körbar", "defekt motor", "trasig motor", "trasig växellåda",
    "växellådsfel", "växellådshaveri", "kolvhaveri", "kamremsbrott",
    "kamkedjebrott", "turboskada", "turbohaveri", "rostskada", "rosthål",
    "underkänd besiktning", "ej godkänd besiktning", "körförbud",
    "reservdelsbil", "för reservdelar", "endast reservdelar", "havererad",
    "vattenskada", "brandskada", "stöldskadad", "deformerad ram",
    "läcker olja kraftigt", "säljes i befintligt skick", "defekt växellåda",
]

# Kända, väldokumenterade svagheter – motor, växellåda, elektronik, fjädring,
# rost m.m. "matchar" testas som delsträngar mot annonsens sammanslagna text
# (titel+beskrivning+specs); tom lista = inget textkrav, bara märke/modell
# räcker. "marken"/"modeller" begränsar vilka bilar varningen gäller för
# (tom lista = alla). Källa: allmänt kända, väldokumenterade svagheter som
# ofta diskuteras bland mekaniker och bilentusiaster – inte en garanti att
# just DEN HÄR bilen har problemet, bara något värt att fråga om/kolla.
KANDA_PROBLEM = [
    # --- Motor ---
    {
        "matchar": ["vti", "thp", "puretech", "ep3", "ep6", "prince-motor",
                    "1.2 pure", "1.4 vti", "1.6 vti", "1.6 thp"],
        "marken": ["citroën", "citroen", "peugeot", "mini", "bmw"], "modeller": [],
        "varning": (
            "Motor: VTi/THP/PureTech (PSA/BMW-MINI) är känd för kamkedje- och "
            "kedjespännarproblem samt hög oljeförbrukning runt 15 000–18 000 mil. "
            "Fråga om kamkedjan bytts."
        ),
    },
    {
        "matchar": ["1.2 tsi", "1.4 tsi", "tsi 122", "tsi 140", "ea111", "ea211"],
        "marken": ["volkswagen", "audi", "seat", "skoda", "škoda"], "modeller": [],
        "varning": (
            "Motor: äldre TSI (före ca 2015) i VAG-koncernen är kända för "
            "kamkedjeproblem där kedjespännaren kan gå sönder. Fråga om kamkedjebyte."
        ),
    },
    {
        "matchar": ["2.0 tdi", "1.6 tdi", "ea189"],
        "marken": ["volkswagen", "audi", "seat", "skoda", "škoda"], "modeller": [],
        "varning": (
            "Motor: 2009–2015 kan vara berörd av dieselskandalen (AdBlue/utsläppsfusk). "
            "Kolla om åtgärdande mjukvaruuppdatering är gjord."
        ),
    },
    {
        "matchar": ["1.0 ecoboost", "1,0 ecoboost"],
        "marken": ["ford"], "modeller": [],
        "varning": (
            "Motor: 1.0 EcoBoost (särskilt 2012–2016) känd för topplockspackningsproblem "
            "och att kamremmen (i oljebad) kan lossna. Kolla servicehistoriken noga."
        ),
    },
    {
        "matchar": ["2.4d", "2,4d", " d5"],
        "marken": ["volvo"], "modeller": [],
        "varning": (
            "Motor: äldre Volvo D5 (5-cyl diesel, före ca 2012) kan drabbas av "
            "insprutningspump-/insprutarhaverier som kan sprida metallpartiklar i motorn. "
            "Fråga om insprutningssystemet servats."
        ),
    },
    {
        "matchar": ["n47", "120d", "320d", "520d"],
        "marken": ["bmw"], "modeller": [],
        "varning": (
            "Motor: BMW N47-diesel (ca 2007–2014) känd för kamkedjeproblem där kedjan kan "
            "gå av i förtid. Fråga om kedjebyte gjorts."
        ),
    },
    {
        "matchar": ["1.4 turbo", "1.4t"],
        "marken": ["opel", "vauxhall", "chevrolet", "saab"], "modeller": [],
        "varning": (
            "Motor: denna 1.4-turbo delar konstruktion med andra kamkedjedrivna "
            "småmotorer med kända förtidiga kedjeproblem."
        ),
    },
    {
        "matchar": ["multiair", "1.4 multiair"],
        "marken": ["fiat", "alfa romeo", "jeep"], "modeller": [],
        "varning": (
            "Motor: MultiAir har ett hydrauliskt ventilstyrningssystem känt för dyra "
            "reparationer vid fel. Lyssna efter ojämn tomgång."
        ),
    },
    {
        "matchar": ["turbo"],
        "marken": ["saab"], "modeller": [],
        "varning": (
            "Motor: Saabs turbomotorer (B234/B235) kräver regelbundna oljebyten – "
            "sludge/turboproblem är vanligt vid eftersatt underhåll. Fråga om servicehistorik."
        ),
    },
    # --- Växellåda ---
    {
        "matchar": ["dsg", "s-tronic", "dq200", "dq250"],
        "marken": ["volkswagen", "audi", "seat", "skoda", "škoda"], "modeller": [],
        "varning": (
            "Växellåda: DSG (särskilt torrkopplad DQ200) är känd för kopplings-/"
            "mekatronikfel, framför allt vid mycket stadskörning. Fråga om löpande "
            "oljebyten på växellådan gjorts (rekommenderas trots 'livstidsolja')."
        ),
    },
    {
        "matchar": ["powershift", "powrshift"],
        "marken": ["ford"], "modeller": [],
        "varning": (
            "Växellåda: Fords PowerShift-automat (ca 2011–2016, t.ex. Focus/C-Max/Fiesta) "
            "är ökänd för kopplings- och mekatronikhaverier. Provkör extra noga och känn "
            "efter ryck/skakningar."
        ),
    },
    {
        "matchar": ["cvt", "xtronic", "variomatic"],
        "marken": ["nissan", "renault", "dacia", "subaru"], "modeller": [],
        "varning": (
            "Växellåda: CVT-lådor kan överhettas och slitas i förtid om inte "
            "växellådsoljan bytts regelbundet. Fråga om serviceintervallen följts."
        ),
    },
    # --- Fjädring ---
    {
        "matchar": [], "marken": ["citroën", "citroen", "peugeot"],
        "modeller": ["c5", "c6", "xantia", "c4 picasso", "407", "607"],
        "varning": (
            "Fjädring: den här modellen kan ha hydraulisk/hydropneumatisk fjädring "
            "som är känd för att läcka eller sluta fungera med åldern – kostsam "
            "reparation. Kontrollera fjädringens funktion och nivå vid provkörning."
        ),
    },
    {
        "matchar": ["luftfjädring", "air suspension"],
        "marken": ["land rover", "range rover", "mercedes-benz", "audi", "volkswagen"],
        "modeller": [],
        "varning": (
            "Fjädring: luftfjädring är dyr att reparera vid läckage (kompressor/"
            "luftkuddar). Kontrollera att bilen står jämnt och inte 'sjunker' över natten."
        ),
    },
    # --- Elektronik ---
    {
        "matchar": [], "marken": ["renault"], "modeller": ["megane", "scenic", "laguna"],
        "varning": (
            "Elektronik: äldre generationer av denna modell (särskilt tidigt 2000-tal) "
            "kan ha problem med centralelektroniken (UCH) som styr bl.a. start och "
            "elfönster. Testa alla elfunktioner noga vid visning."
        ),
    },
    {
        "matchar": [], "marken": ["land rover", "range rover"], "modeller": [],
        "varning": (
            "Elektronik: dessa modeller har rykte om sig att ha diverse elektronikfel "
            "(varningslampor, sensorer). Kolla att inga varningslampor lyser och fråga "
            "om senaste diagnossökning."
        ),
    },
    # --- Rost/kaross ---
    {
        "matchar": [], "marken": ["alfa romeo"], "modeller": [],
        "varning": (
            "Kaross: äldre Alfa Romeo-modeller (särskilt före ca 2005) har ett känt "
            "rostrykte. Kolla extra noga under trösklar, hjulhus och innerskärmar."
        ),
    },
    {
        "matchar": [], "marken": ["saab"], "modeller": ["9-3", "9-5", "900", "9000"],
        "varning": (
            "Kaross: kolla rost vid bakre hjulhus, trösklar och fjäderben-infästningar "
            "– vanliga rostställen på äldre Saab i svenskt klimat."
        ),
    },
]

# Enkla nyckelord i annonstexten som räknas som "plus" respektive "minus"
# i den automatiska snabbanalysen.
PLUS_SIGNALER = [
    ("nybesiktigad", "Nyligen besiktigad"),
    ("besiktigad till", "Har aktuell besiktning angiven"),
    ("besiktigad t.o.m", "Har aktuell besiktning angiven"),
    ("ny kamkedja", "Kamkedja nybytt"),
    ("bytt kamkedja", "Kamkedja nybytt"),
    ("ny kamrem", "Kamrem nybytt"),
    ("bytt kamrem", "Kamrem nybytt"),
    ("kamremsbyte", "Kamrem nybytt"),
    ("kamrem bytt", "Kamrem nybytt"),
    ("kamrem och vattenpump", "Kamrem (och vattenpump) nybytt"),
    ("nya bromsar", "Nya bromsar"),
    ("bytta bromsar", "Bromsar utbytta"),
    ("nya länkarmar", "Nya länkarmar"),
    ("nya styrleder", "Nya styrleder"),
    ("nya hjullager", "Nya hjullager"),
    ("nya fjädrar", "Nya fjädrar"),
    ("nya däck", "Nya däck"),
    ("nyservad", "Nyligen servad"),
    ("nyservice", "Nyligen servad"),
    ("dragkrok", "Har dragkrok"),
    ("takata", "Krockkudderecall (Takata) åtgärdad"),
    ("är bytta", "Vissa delar/komponenter utbytta enligt annonsen (läs vilka i texten)"),
    ("är utbytt", "Vissa delar/komponenter utbytta enligt annonsen (läs vilka i texten)"),
    ("nybytt", "Har nybytta delar enligt annonsen"),
]

MINUS_SIGNALER = [
    ("spricka i vindrutan", "Spricka i vindrutan"),
    ("stenskott", "Stenskott i vindrutan"),
    ("rost", "Rost nämnt i annonsen"),
    ("oljeläck", "Oljeläckage nämnt"),
    ("ac funkar ej", "AC fungerar ej"),
    ("ej fungerande ac", "AC fungerar ej"),
    ("motorlampa", "Motorlampa lyser/har lyst"),
    ("anmärkning", "Besiktigad med anmärkning"),
    ("tusen mil kvar", "Säljaren nämner begränsad återstående livslängd på en del"),
]


def _traff_utan_negation(kombinerad: str, nyckel: str) -> bool:
    """
    Sant om 'nyckel' finns i texten UTAN att direkt föregås av ett negationsord
    (t.ex. "inga anmärkningar" eller "ingen rost" ska INTE räknas som en träff).
    """
    negationer = ("inga ", "ingen ", "utan ", "ej ", "inte ", "fri från ", "fritt från ")
    fonster = 15
    start = 0
    while True:
        idx = kombinerad.find(nyckel, start)
        if idx == -1:
            return False
        foregaende = kombinerad[max(0, idx - fonster):idx]
        if not any(neg in foregaende for neg in negationer):
            return True
        start = idx + len(nyckel)


def analysera_djupare(annons: Annons) -> dict:
    """
    Hämtar hela annonssidan EN gång och gör en regelbaserad djupanalys.
    Returnerar {} om annonsen inte gick att hämta (nätverksfel etc – vi
    struntar då i djupanalysen för just den annonsen istället för att krascha).
    """
    try:
        ad_id = int(annons.id)
    except ValueError:
        return {}
    try:
        data = BlocketAPI().get_ad(CarAd(id=ad_id))
    except Exception:
        return {}

    titel = str(data.get("title", ""))
    beskrivning = str(data.get("description", ""))
    specifikationer = data.get("specifications") or {}
    utrustning = " ".join(data.get("equipment", []) or [])
    kombinerad = " ".join([
        titel, beskrivning, utrustning,
        " ".join(str(v) for v in specifikationer.values()),
    ]).lower()

    skadeord_traff = next(
        (o for o in [w.lower() for w in STANDARD_SKADEORD] if _traff_utan_negation(kombinerad, o)), None
    )

    marke_lower = (annons.marke or "").lower()
    modell_lower = (annons.modell or "").lower()
    kanda_problem = []
    for post in KANDA_PROBLEM:
        if post["marken"] and not any(m in marke_lower for m in post["marken"]):
            continue
        if post["modeller"] and not any(mo in modell_lower for mo in post["modeller"]):
            continue
        # Tomt "matchar" = inget textkrav (räcker med märke/modell-träff ovan)
        if post["matchar"] and not any(_traff_utan_negation(kombinerad, trigger) for trigger in post["matchar"]):
            continue
        kanda_problem.append(post["varning"])

    plus = [text for nyckel, text in PLUS_SIGNALER if _traff_utan_negation(kombinerad, nyckel)]
    minus = [text for nyckel, text in MINUS_SIGNALER if _traff_utan_negation(kombinerad, nyckel)]

    antal_agare = None
    m = re.search(r"(\d+)\s*(?:tidigare\s*|st\s*)?ägare", kombinerad)
    if m:
        antal_agare = int(m.group(1))
        if antal_agare >= 6:
            minus.append(f"{antal_agare} tidigare ägare – ovanligt många för miltalet")
        elif antal_agare <= 2:
            plus.append(f"Bara {antal_agare} tidigare ägare")

    return {
        "skadeord_traff": skadeord_traff,
        "kanda_problem": kanda_problem,
        "plus": plus,
        "minus": minus,
        "antal_agare": antal_agare,
    }


# --------------------------------------------------------------------------
# Universella kontroller – gäller ALLA bilar av en viss typ (t.ex. drivmedel),
# oavsett märke/modell. Kräver ingen sidhämtning eftersom drivmedel redan
# finns i sökresultatet.
# --------------------------------------------------------------------------
def tillampa_universella_kontroller(annons: Annons, info: dict, cfg: dict) -> None:
    analys = cfg.get("analys", {})
    drivmedel = (annons.drivmedel or "").lower()

    # Diesel: visas som en neutral upplysning – påverkar INTE om bilen räknas
    # som toppkap eller ej (du har sagt att du inte vill sortera bort diesel).
    if "diesel" in drivmedel and analys.get("varna_diesel_korta_strackor", True):
        info.setdefault("info", []).append(
            "Diesel: partikelfiltret (FAP/DPF) mår bäst av regelbundna, längre körsträckor "
            "(gärna motorväg) för att rensa sig självt. Körs bilen mest korta sträckor i "
            "stadstrafik/vintertid är det värt att hålla koll på. Kolla även exakt "
            "fordonsskatt för just den här bilen (regnr) hos Transportstyrelsen."
        )

    # Kamrem/kamkedja: gäller ALLA bilar oavsett drivmedel/märke. Om miltalet är
    # högt och annonsen inte nämner att den bytts, är det en verklig kostnadsrisk
    # (ett brott kan förstöra motorn) – räknas som en riktig anmärkning.
    grans = analys.get("kamrem_varningsgrans_mil", 15000)
    plus_lista = info.get("plus") or []
    redan_bytt = any("kamrem" in p.lower() or "kamkedja" in p.lower() for p in plus_lista)
    if annons.miltal is not None and annons.miltal >= grans and not redan_bytt:
        info.setdefault("minus", []).append(
            f"{annons.miltal:,} mil utan att kamrem/kamkedja nämns bytt i annonsen – "
            "slits med tiden och ett brott kan förstöra motorn. Fråga säljaren om/när "
            "den senast byttes.".replace(",", " ")
        )


def analysera_kandidater(
    kap: list[tuple[Annons, float, int]], cfg: dict
) -> tuple[list[tuple[Annons, float, int, dict]], list[tuple[Annons, float, int, str]]]:
    """
    Kör analysera_djupare() på varje kap-kandidat, plus universella kontroller
    (t.ex. diesel+korta sträckor) som inte kräver att sidan hämtas.
    Returnerar (ok_kap, skadade_kap):
      - ok_kap: (annons, median, procent, djupanalys)
      - skadade_kap: (annons, median, procent, matchat_skadeord)
    """
    analys = cfg.get("analys", {})
    kor_djupanalys = analys.get("kontrollera_skador", True) or analys.get("djupanalys", True)

    max_kontroller = analys.get("max_skadekontroller_per_korning", 150)

    ok: list[tuple[Annons, float, int, dict]] = []
    skadade: list[tuple[Annons, float, int, str]] = []

    for i, (annons, median, procent) in enumerate(kap):
        if not kor_djupanalys or i >= max_kontroller:
            info: dict = {}
            tillampa_universella_kontroller(annons, info, cfg)
            ok.append((annons, median, procent, info))
            continue

        info = analysera_djupare(annons)
        if info.get("skadeord_traff"):
            skadade.append((annons, median, procent, info["skadeord_traff"]))
        else:
            tillampa_universella_kontroller(annons, info, cfg)
            ok.append((annons, median, procent, info))

        time.sleep(0.5)  # var snäll mot Blockets servrar – en sida per kandidat

    return ok, skadade


def dela_in_i_nivaer(
    ok_kap: list[tuple[Annons, float, int, dict]],
) -> tuple[list[tuple[Annons, float, int, dict]], list[tuple[Annons, float, int, dict]]]:
    """
    Delar upp de "friska" kap-kandidaterna i två nivåer:
      - toppkap: inga kända märkes-/modellsvagheter matchade och högst en minus-punkt
      - med_anmarkning: har minst en känd svaghet ELLER flera minus-punkter
    Prioriterar "hellre färre men bra bilar" – toppkap ska vara de du kan lita mest på.
    """
    toppkap = []
    med_anmarkning = []
    for annons, median, procent, info in ok_kap:
        har_kant_problem = bool(info.get("kanda_problem"))
        antal_minus = len(info.get("minus") or [])
        if har_kant_problem or antal_minus >= 2:
            med_anmarkning.append((annons, median, procent, info))
        else:
            toppkap.append((annons, median, procent, info))
    return toppkap, med_anmarkning


# --------------------------------------------------------------------------
# Lokal resultatsida (HTML) – öppnas i webbläsaren, ingen mejlkonfiguration behövs
# --------------------------------------------------------------------------
RESULTAT_PATH = HAR / "docs" / "index.html"  # GitHub Pages serverar från docs/-mappen


def _kap_kort_html(annons: Annons, median: float, procent: int, info: dict, nya_ids: set[str]) -> str:
    nytt_badge = '<span class="badge">NYTT</span>' if annons.id in nya_ids else ""
    regnr_html = (
        f'<div class="regnr">Regnr: <b>{annons.regnr}</b> '
        f'(slå upp värdering manuellt på car.info)</div>' if annons.regnr else ""
    )

    djupanalys_html = ""
    kanda_problem = info.get("kanda_problem") or []
    plus = info.get("plus") or []
    minus = info.get("minus") or []
    neutral_info = info.get("info") or []
    if kanda_problem or plus or minus or neutral_info:
        problem_html = "".join(f'<div class="problemvarning">⚠️ {v}</div>' for v in kanda_problem)
        info_html = "".join(f'<div class="neutralinfo">ℹ️ {v}</div>' for v in neutral_info)
        plus_html = "".join(f'<li class="plus">+ {p}</li>' for p in plus)
        minus_html = "".join(f'<li class="minus">− {m}</li>' for m in minus)
        lista_html = f'<ul class="plusminus">{plus_html}{minus_html}</ul>' if (plus or minus) else ""
        djupanalys_html = f"""
        <div class="djupanalys">
          <div class="djupanalys-etikett">Automatisk snabbanalys (heuristik – dubbelkolla alltid själv)</div>
          {problem_html}
          {info_html}
          {lista_html}
        </div>"""

    return f"""
    <div class="kort">
      <div class="titelrad">{nytt_badge}<a href="{annons.url}" target="_blank">{annons.titel}</a></div>
      <div class="pris">{annons.pris:,} kr <span class="procent">– {procent}% under liknande annonsers median ({int(median):,} kr)</span></div>
      <div class="detaljer">Årsmodell: {annons.ar or '?'} &nbsp;·&nbsp; Miltal: {annons.miltal or '?'} mil &nbsp;·&nbsp; Plats: {annons.plats or '?'}</div>
      {regnr_html}
      {djupanalys_html}
    </div>""".replace(",", " ")


def skriv_html_rapport(
    toppkap: list[tuple[Annons, float, int, dict]],
    med_anmarkning: list[tuple[Annons, float, int, dict]],
    nya_ids: set[str],
    skadade: list[tuple[Annons, float, int, str]],
    cfg: dict,
) -> None:
    toppkap_html = "".join(_kap_kort_html(a, m, p, i, nya_ids) for a, m, p, i in toppkap)
    anmarkning_html = "".join(_kap_kort_html(a, m, p, i, nya_ids) for a, m, p, i in med_anmarkning)

    anmarkning_sektion = ""
    if med_anmarkning:
        anmarkning_sektion = f"""
        <h2>🔧 Kap med anmärkning ({len(med_anmarkning)})</h2>
        <div class="info">Fortfarande riktigt billiga, men annonsen nämner något värt att
        kolla upp extra noga (känd märkessvaghet eller flera anmärkningar) innan du slår till.</div>
        {anmarkning_html}
        """

    skadade_html = []
    for annons, median, procent, ord_ in skadade:
        skadade_html.append(f"""
        <div class="kort skadad">
          <div class="titelrad"><span class="badge badge-varning">⚠️ {ord_}</span><a href="{annons.url}" target="_blank">{annons.titel}</a></div>
          <div class="pris">{annons.pris:,} kr <span class="procent">– {procent}% under median, men troligen skadad/trasig</span></div>
          <div class="detaljer">Årsmodell: {annons.ar or '?'} &nbsp;·&nbsp; Miltal: {annons.miltal or '?'} mil &nbsp;·&nbsp; Plats: {annons.plats or '?'}</div>
        </div>""".replace(",", " "))

    skadade_sektion = ""
    if skadade_html:
        skadade_sektion = f"""
        <h2>🚫 Uteslutna – troligen skadade/trasiga ({len(skadade_html)})</h2>
        <div class="info">Dessa var billiga men annonstexten innehöll ord som tyder på skada/fel,
        så de räknas inte som riktiga kap – korrekt prissatta för sitt skick helt enkelt.
        Listade ändå ifall du är ute efter ett renoveringsobjekt.</div>
        {"".join(skadade_html)}
        """

    totalt = len(toppkap) + len(med_anmarkning)

    html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="utf-8">
<title>Bilbevakning – kap på Blocket</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f4f4f2; margin: 0; padding: 24px; color: #1a1a1a; }}
  h1 {{ font-size: 22px; }}
  h2 {{ font-size: 18px; margin-top: 32px; }}
  .uppdaterad {{ color: #666; font-size: 14px; margin-bottom: 20px; }}
  .info {{ color: #666; font-size: 13px; margin-bottom: 14px; }}
  .kort {{ background: white; border-radius: 10px; padding: 16px 18px; margin-bottom: 12px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .kort.skadad {{ opacity: 0.75; }}
  .titelrad {{ font-size: 17px; font-weight: 600; margin-bottom: 4px; }}
  .titelrad a {{ color: #1a1a1a; text-decoration: none; }}
  .titelrad a:hover {{ text-decoration: underline; }}
  .badge {{ background: #ff5a36; color: white; font-size: 11px; font-weight: 700;
            padding: 2px 8px; border-radius: 999px; margin-right: 8px; vertical-align: middle; }}
  .badge-varning {{ background: #a1631f; }}
  .pris {{ font-size: 16px; font-weight: 600; color: #1a7f37; margin-bottom: 4px; }}
  .procent {{ color: #555; font-weight: 400; font-size: 14px; }}
  .detaljer {{ color: #444; font-size: 14px; }}
  .regnr {{ color: #444; font-size: 13px; margin-top: 4px; }}
  .tom {{ color: #666; }}
  .djupanalys {{ margin-top: 10px; padding-top: 10px; border-top: 1px solid #eee; }}
  .djupanalys-etikett {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: .03em; margin-bottom: 6px; }}
  .problemvarning {{ background: #fff4e5; border-left: 3px solid #c77700; padding: 8px 10px; border-radius: 6px;
                    font-size: 13px; margin-bottom: 6px; }}
  .neutralinfo {{ background: #eef3f9; border-left: 3px solid #5b7ba8; padding: 8px 10px; border-radius: 6px;
                    font-size: 13px; margin-bottom: 6px; color: #444; }}
  ul.plusminus {{ list-style: none; padding: 0; margin: 4px 0 0 0; font-size: 13px; }}
  ul.plusminus li {{ padding: 2px 0; }}
  li.plus {{ color: #1a7f37; }}
  li.minus {{ color: #b42318; }}
</style>
</head>
<body>
  <h1>🚗 Bilbevakning – möjliga kap</h1>
  <div class="uppdaterad">Senast uppdaterad: {datetime.now():%Y-%m-%d %H:%M:%S} &nbsp;·&nbsp; {totalt} kap hittade totalt</div>
  <h2>🏆 Toppkap ({len(toppkap)})</h2>
  <div class="info">Inga kända märkes-/modellsvagheter matchade och högst en anmärkning i texten – de säkraste korten.</div>
  {toppkap_html if toppkap_html else '<p class="tom">Inga toppkap just nu.</p>'}
  {anmarkning_sektion}
  {skadade_sektion}
</body>
</html>"""

    RESULTAT_PATH.parent.mkdir(parents=True, exist_ok=True)  # säkerställ att docs/-mappen finns
    with open(RESULTAT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


# --------------------------------------------------------------------------
# E-post (valfritt – avstängt som standard, se config.json "epost.aktiverat")
# --------------------------------------------------------------------------
def skicka_mail(kap: list[tuple[Annons, float, int, dict]], cfg: dict) -> None:
    e = cfg["epost"]
    rader = []
    for annons, median, procent, info in kap:
        regnr_rad = f"   Regnr: {annons.regnr}  (slå upp värdering manuellt på car.info)\n" if annons.regnr else ""
        rader.append(
            f"🚗 {annons.titel}\n"
            f"   Pris: {annons.pris:,} kr  (ca {procent}% under liknande annonsers "
            f"medianpris på {int(median):,} kr)\n"
            f"   Årsmodell: {annons.ar or '?'}   Miltal: {annons.miltal or '?'}   "
            f"Plats: {annons.plats or '?'}\n"
            f"{regnr_rad}"
            f"   {annons.url}\n"
        )
    kropp = (
        f"Hittade {len(kap)} möjliga kap ({datetime.now():%Y-%m-%d %H:%M}):\n\n"
        + "\n".join(rader)
        + "\n\nKom ihåg: dubbelkolla alltid skick, historik och ev. regnr "
          "(t.ex. via car.info) innan du hör av dig till säljaren."
    )

    msg = MIMEText(kropp, _charset="utf-8")
    msg["Subject"] = f"🚗 {len(kap)} möjliga bilkap hittade på Blocket"
    msg["From"] = e["avsandare"]
    msg["To"] = e["mottagare"]

    port = int(e["smtp_port"])
    if port == 465:
        # SSL rakt av (t.ex. Gmail)
        with smtplib.SMTP_SSL(e["smtp_server"], port) as server:
            server.login(e["avsandare"], e["app_losenord"])
            server.send_message(msg)
    else:
        # STARTTLS (t.ex. Outlook/Hotmail på port 587)
        with smtplib.SMTP(e["smtp_server"], port) as server:
            server.starttls()
            server.login(e["avsandare"], e["app_losenord"])
            server.send_message(msg)

    print(f"📧 Mail skickat med {len(kap)} annonser.")


# --------------------------------------------------------------------------
# Huvudloop
# --------------------------------------------------------------------------
def kor_en_sokning(spara_raw: bool = False) -> None:
    cfg = las_config()
    sedda = las_state()

    print(f"🔍 Söker på Blocket... ({datetime.now():%Y-%m-%d %H:%M:%S})")
    annonser = hamta_annonser(cfg, spara_raw=spara_raw)
    print(f"   Hittade {len(annonser)} annonser totalt som matchar kriterierna.")

    kap = hitta_kap(annonser, cfg)
    print(f"   {len(kap)} möjliga kap innan djupanalys. Kontrollerar annonstexter "
          f"(skador, kända märkes-/modellsvagheter, plus/minus)...")
    ok_kap, skadade = analysera_kandidater(kap, cfg)
    if skadade:
        print(f"   ⚠️  {len(skadade)} av dem verkar skadade/trasiga – uteslutna från kap-listan.")

    toppkap, med_anmarkning = dela_in_i_nivaer(ok_kap)
    alla_kap = toppkap + med_anmarkning
    print(f"   🏆 {len(toppkap)} toppkap, 🔧 {len(med_anmarkning)} med anmärkning.")

    nya_ids = {a.id for (a, _, _, _) in alla_kap if a.id not in sedda}

    # Skriv/uppdatera den lokala resultatsidan – oavsett om mejl är påslaget eller ej
    skriv_html_rapport(toppkap, med_anmarkning, nya_ids, skadade, cfg)
    print(f"   {len(alla_kap)} kap totalt ({len(nya_ids)} nya sedan sist). "
          f"Resultatsida: {RESULTAT_PATH}")

    visning = cfg.get("visning", {})
    if nya_ids and visning.get("oppna_automatiskt_vid_nya_kap", True):
        try:
            subprocess.run(["open", str(RESULTAT_PATH)], check=False)
        except Exception:
            pass

    epost_cfg = cfg.get("epost", {})
    if nya_ids and epost_cfg.get("aktiverat", False):
        nya_kap = [(a, m, p, info) for (a, m, p, info) in alla_kap if a.id in nya_ids]
        try:
            skicka_mail(nya_kap, cfg)
        except Exception as e:
            print(f"⚠️  Kunde inte skicka mail (resultatsidan uppdaterades ändå): {e}")

    # Uppdatera "sedda" så NYTT-märkningen blir korrekt nästa gång
    sedda.update(a.id for (a, _, _, _) in alla_kap)
    spara_state(sedda)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bilbevakning för Blocket")
    parser.add_argument("--once", action="store_true", help="Kör en enda sökning och avsluta")
    parser.add_argument("--debug-raw", action="store_true",
                         help="Spara rådata från Blockets API till debug_raw.json")
    args = parser.parse_args()

    if args.once:
        kor_en_sokning(spara_raw=args.debug_raw)
        return

    cfg = las_config()
    intervall_h = cfg["korning"].get("kontrollera_varje_timme", 1)
    print(f"▶️  Startar bevakning, kollar var {intervall_h}:e timme. Avbryt med Ctrl+C.")
    while True:
        try:
            kor_en_sokning(spara_raw=args.debug_raw)
        except Exception as e:
            print(f"❌ Oväntat fel: {e}")
        print(f"😴 Väntar {intervall_h} timme(ar) till nästa sökning...\n")
        time.sleep(intervall_h * 3600)


if __name__ == "__main__":
    main()
