"""
Synkar metadata från KB:s Publicera-plattform till PostgreSQL via OAI-PMH.

Hämtar Dublin Core-metadata för alla tidskrifter med relevans 1 eller 2
(definierade i 01_inventera_tidskrifter.py) med:
- Paginering via resumptionToken
- Inkrementell synk via OAI-PMH from-parameter (hämtar bara nytt/ändrat)
- Tillståndsbaserad checkpoint i sync_status-tabell
- Pause mellan anrop för att inte belasta servern

Kör (första gången — full synk):
    python3 02_synka_metadata.py

Kör (inkrementell — bara nytt sedan senaste körning):
    python3 02_synka_metadata.py

Tvinga full omsynk av en specifik tidskrift:
    python3 02_synka_metadata.py --force --tidskrift sjpa

Tvinga full omsynk av alla:
    python3 02_synka_metadata.py --force

Installera dagligt schemalagt jobb (launchd på macOS, cron på Linux):
    python3 02_synka_metadata.py --installera-schema
"""

import argparse
import logging
import os
import platform
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).parent.resolve()

# --- Konfiguration -----------------------------------------------------------

PUBLICERA_TIMEOUT = int(os.getenv("PUBLICERA_TIMEOUT", "30"))
PUBLICERA_PAUS    = float(os.getenv("PUBLICERA_PAUS_SEKUNDER", "0.5"))

# Kommaseparerade förkortningar att synka; tomt = alla med relevans 1-2
TIDSKRIFTER_FILTER = [
    t.strip()
    for t in os.getenv("PUBLICERA_TIDSKRIFTER", "").split(",")
    if t.strip()
]

# DDK-avdelningar att indexera (styr även cache-rensning vid synk)
_ddk_ravar = os.getenv("PUBLICERA_DDK_FILTER", "")
DDK_FILTER: list[str] = [c.strip() for c in _ddk_ravar.split(",") if c.strip()]

NS_OAI  = "http://www.openarchives.org/OAI/2.0/"
NS_DC   = "http://purl.org/dc/elements/1.1/"
NS_OADC = "http://www.openarchives.org/OAI/2.0/oai_dc/"

# Tidskrifter med relevans 1 (hög) och 2 (medel) som synkas som standard
# (spec, namn, relevans, ddk_avdelning)
RELEVANTA_TIDSKRIFTER: list[tuple[str, str, int, str]] = [
    ("ejels",      "European Journal of Empirical Legal Studies",                        1, "340"),
    ("siplr",      "Stockholm Intellectual Property Law Review",                          1, "340"),
    ("sjpa",       "Scandinavian Journal of Public Administration",                       1, "350"),
    ("socvet",     "Socialvetenskaplig tidskrift",                                        1, "300"),
    ("jdsr",       "Journal of Digital Social Research",                                  1, "300"),
    ("ihs",        "Idrott, historia och samhälle",                                       1, "300"),
    ("fornvannen", "Fornvännen",                                                          2, "900"),
    ("csa",        "Current Swedish Archaeology",                                         2, "900"),
    ("arv",        "Arv",                                                                 2, "390"),
    ("ethsc",      "Ethnologia Scandinavica",                                             2, "390"),
    ("meta",       "META – Historiskarkeologisk tidskrift",                               2, "900"),
    ("rig",        "Rig. Kulturhistorisk tidskrift",                                      2, "900"),
    ("fn",         "Forum navale",                                                        2, "350"),
    ("kp",         "Kulturella Perspektiv – Svensk etnologisk tidskrift",                 2, "390"),
    ("smt",        "Socialmedicinsk tidskrift",                                           2, "360"),
    ("tgv",        "Tidskrift för genusvetenskap",                                        2, "300"),
    ("insi",       "In Situ Archaeologica",                                               2, "900"),
    ("jonas",      "Journal of Nordic Archaeological Science – JONAS",                    2, "900"),
    ("opuscula",   "Opuscula. Annual of the Swedish Institutes at Athens and Rome",       2, "930"),
    ("shs",        "Släkthistoriska Studier",                                             2, "920"),
]


# --- Databashjälpare ---------------------------------------------------------

def db_anslut():
    """Ansluter till PostgreSQL och returnerar en psycopg2-anslutning."""
    import psycopg2
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url or database_url.startswith("sqlite"):
        sys.exit("FEL: DATABASE_URL saknas eller är SQLite. Synk kräver PostgreSQL.")
    return psycopg2.connect(database_url)


def skapa_schema(kon) -> None:
    """
    Skapar schema och tabeller om de inte redan finns.

    Schemat är auktoritativt definierat i mcp_server.py:ensure_schema() —
    denna funktion är en PG-specifik spegel för synk-skriptets behov.
    Bas-schemat (v1.0.0) ska inte ändras efter publicering; nya kolumner
    läggs till via migration-block längst ner.
    """
    with kon:
        with kon.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS publicera_kb")

            # Tidskrift-register — populeras av upsert_tidskrifter() nedan
            cur.execute("""
                CREATE TABLE IF NOT EXISTS publicera_kb.tidskrift (
                    spec          TEXT PRIMARY KEY,
                    namn          TEXT NOT NULL,
                    ddk_avdelning TEXT,
                    ddk_kod       TEXT,
                    sao_amnesord  TEXT[],
                    antal_poster  INTEGER,
                    synkad        BOOLEAN DEFAULT FALSE,
                    uppdaterad    TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS publicera_kb.artikel (
                    oai_id          TEXT PRIMARY KEY,
                    spec            TEXT NOT NULL,
                    artikel_url     TEXT,
                    doi             TEXT,
                    pdf_url         TEXT,
                    titel           TEXT,
                    forfattare      TEXT[],
                    amnesord        TEXT[],
                    abstrakt        TEXT,
                    publicerad      DATE,
                    tidskrift_namn  TEXT,
                    volym_nummer    TEXT,
                    issn            TEXT,
                    sprak           TEXT,
                    licens          TEXT,
                    datestamp       TIMESTAMPTZ,
                    fulltext        TEXT,
                    fulltext_format TEXT,
                    fulltext_cachad TIMESTAMPTZ,
                    indexerad       TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS artikel_spec_idx
                    ON publicera_kb.artikel (spec)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS artikel_publicerad_idx
                    ON publicera_kb.artikel (publicerad DESC NULLS LAST)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS artikel_sprak_idx
                    ON publicera_kb.artikel (sprak)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS publicera_kb.sync_status (
                    spec            TEXT PRIMARY KEY,
                    senast_synkad   TIMESTAMPTZ,
                    antal_poster    INTEGER DEFAULT 0,
                    status          TEXT DEFAULT 'klar'
                )
            """)

    # -- migrationer after publication -----------------------------------------
    # M1: Konvertera fts_tsv till generated column med 'simple' (v2.0.0, 2026-05-22)
    #     Blandspråkig korpus (sv + en) — 'simple' ger korrekt matchning utan stemming.
    #     Generated column underhålls automatiskt av PostgreSQL vid upsert.
    with kon:
        with kon.cursor() as cur:
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'publicera_kb'
                          AND table_name   = 'artikel'
                          AND column_name  = 'fts_tsv'
                          AND is_generated = 'ALWAYS'
                    ) THEN
                        ALTER TABLE publicera_kb.artikel DROP COLUMN IF EXISTS fts_tsv;
                        ALTER TABLE publicera_kb.artikel
                            ADD COLUMN fts_tsv TSVECTOR
                            GENERATED ALWAYS AS (
                                to_tsvector('simple'::regconfig,
                                    coalesce(titel,'') || ' ' || coalesce(abstrakt,''))
                            ) STORED;
                    END IF;
                END$$
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS artikel_fts_idx
                    ON publicera_kb.artikel USING GIN (fts_tsv)
            """)


def rensa_utgangna_tidskrifter(kon, aktiv_ddk: list[str]) -> int:
    """
    Tar bort artiklar och sync_status för tidskrifter vars DDK-avdelning
    inte längre finns i aktiv_ddk (PUBLICERA_DDK_FILTER).

    Anropas automatiskt vid varje synk. Om PUBLICERA_DDK_FILTER är tomt
    (= alla avdelningar aktiva) görs ingen rensning.

    Returnerar antal borttagna artiklar.
    """
    if not aktiv_ddk:
        return 0  # Tomt filter = inga begränsningar — inget att rensa

    with kon:
        with kon.cursor() as cur:
            placeholders = ",".join(["%s"] * len(aktiv_ddk))
            cur.execute(
                f"SELECT spec FROM publicera_kb.tidskrift "
                f"WHERE ddk_avdelning NOT IN ({placeholders}) "
                f"AND ddk_avdelning IS NOT NULL",
                aktiv_ddk,
            )
            utgangna_specs = [r[0] for r in cur.fetchall()]

            if not utgangna_specs:
                return 0

            spec_ph = ",".join(["%s"] * len(utgangna_specs))

            cur.execute(
                f"SELECT COUNT(*) FROM publicera_kb.artikel WHERE spec IN ({spec_ph})",
                utgangna_specs,
            )
            antal = cur.fetchone()[0]

            if antal > 0:
                cur.execute(
                    f"DELETE FROM publicera_kb.artikel WHERE spec IN ({spec_ph})",
                    utgangna_specs,
                )
                cur.execute(
                    f"DELETE FROM publicera_kb.sync_status WHERE spec IN ({spec_ph})",
                    utgangna_specs,
                )

    return antal


def upsert_tidskrifter(kon) -> None:
    """Fyller tidskrift-tabellen från RELEVANTA_TIDSKRIFTER-listan."""
    with kon:
        with kon.cursor() as cur:
            for spec, namn, _relevans, ddc in RELEVANTA_TIDSKRIFTER:
                cur.execute("""
                    INSERT INTO publicera_kb.tidskrift (spec, namn, ddk_avdelning, uppdaterad)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (spec) DO UPDATE SET
                        namn          = EXCLUDED.namn,
                        ddk_avdelning = EXCLUDED.ddk_avdelning,
                        uppdaterad    = NOW()
                """, (spec, namn, ddc))


def hamta_senast_synkad(kon, spec: str) -> str | None:
    """Returnerar ISO-datumsträng (YYYY-MM-DD) för senaste synk, eller None."""
    with kon.cursor() as cur:
        cur.execute(
            "SELECT senast_synkad FROM publicera_kb.sync_status WHERE spec = %s",
            (spec,)
        )
        rad = cur.fetchone()
        if rad and rad[0]:
            return rad[0].strftime("%Y-%m-%d")
    return None


def uppdatera_sync_status(kon, spec: str, antal: int) -> None:
    """Uppdaterar sync_status med nuvarande tidsstämpel och antal poster."""
    with kon:
        with kon.cursor() as cur:
            cur.execute("""
                INSERT INTO publicera_kb.sync_status (spec, senast_synkad, antal_poster)
                VALUES (%s, NOW(), %s)
                ON CONFLICT (spec) DO UPDATE SET
                    senast_synkad = NOW(),
                    antal_poster  = EXCLUDED.antal_poster,
                    status        = 'klar'
            """, (spec, antal))


# --- OAI-PMH-parsning --------------------------------------------------------

def _text_lista(metadata, tag: str, ns: str = NS_DC) -> list[str]:
    """Returnerar lista med texter för alla förekomster av taggen."""
    return [
        e.text.strip()
        for e in metadata.findall(f"{{{ns}}}{tag}")
        if e.text and e.text.strip()
    ]


def tolka_poster(records: list) -> list[dict]:
    """
    Tolkar en lista med OAI-PMH record-element till dicts redo för DB-upsert.

    DC-format på Publicera:
    - dc:identifier  → artikel-URL ELLER DOI (två separata element)
    - dc:relation    → PDF-URL
    - dc:source      → tidskriftsnamn+volym/nummer ELLER ISSN (flera element)
    - dc:subject     → ämnesord (flera element)
    - dc:creator     → författare (flera element)
    - dc:date        → publiceringsdatum (YYYY-MM-DD eller YYYY)
    """
    poster = []
    for rec in records:
        header = rec.find(f"{{{NS_OAI}}}header")
        if header is not None and header.get("status") == "deleted":
            continue

        oai_id    = header.findtext(f"{{{NS_OAI}}}identifier", "").strip()
        datestamp = header.findtext(f"{{{NS_OAI}}}datestamp", "").strip()
        spec      = ""
        for setspec in header.findall(f"{{{NS_OAI}}}setSpec"):
            val = setspec.text or ""
            if ":" not in val and val.strip():
                spec = val.strip()
                break

        metadata = rec.find(f".//{{{NS_OADC}}}dc")
        if metadata is None:
            continue

        identifierare = _text_lista(metadata, "identifier")
        artikel_url = next(
            (u for u in identifierare if u.startswith("http") and "/article/view/" in u and u.count("/") > 5),
            next((u for u in identifierare if u.startswith("http")), "")
        )
        doi = next(
            (u for u in identifierare if u.startswith("10.") or "/doi/" in u or "doi.org" in u),
            ""
        )

        relationer = _text_lista(metadata, "relation")
        pdf_url = next(
            (r for r in relationer if r.startswith("http") and "/article/view/" in r),
            ""
        )

        # dc:source: skilja ut ISSN (bara siffror och bindestreck) från övrig text
        kallor = _text_lista(metadata, "source")
        issn = next(
            (k for k in kallor if k.replace("-", "").isdigit() and len(k) == 9),
            ""
        )
        tidskrift_volym = next(
            (k for k in kallor if not k.replace("-", "").isdigit()),
            ""
        )

        # Publiceringsdatum — len(fmt) är formatlängden, inte datumstränglängden.
        # Korrekt mappning: "%Y-%m-%d" → 10 tecken, "%Y-%m" → 7, "%Y" → 4.
        datum_str = next(iter(_text_lista(metadata, "date")), "")
        publicerad = None
        if datum_str:
            for fmt, lgt in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
                try:
                    dt = datetime.strptime(datum_str[:lgt], fmt)
                    publicerad = dt.date().isoformat()
                    break
                except ValueError:
                    continue

        # Datestamp → timestamptz
        try:
            ts = datetime.fromisoformat(datestamp.replace("Z", "+00:00"))
        except ValueError:
            ts = None

        poster.append({
            "oai_id":         oai_id,
            "spec":           spec,
            "artikel_url":    artikel_url,
            "doi":            doi,
            "pdf_url":        pdf_url,
            "titel":          next(iter(_text_lista(metadata, "title")), ""),
            "forfattare":     _text_lista(metadata, "creator"),
            "amnesord":       _text_lista(metadata, "subject"),
            "abstrakt":       next(iter(_text_lista(metadata, "description")), ""),
            "publicerad":     publicerad,
            "tidskrift_namn": next(iter(_text_lista(metadata, "publisher")), ""),
            "volym_nummer":   tidskrift_volym,
            "issn":           issn,
            "sprak":          next(iter(_text_lista(metadata, "language")), ""),
            "licens":         next(
                (r for r in _text_lista(metadata, "rights") if "creativecommons" in r or "http" in r),
                next(iter(_text_lista(metadata, "rights")), "")
            ),
            "datestamp":      ts,
        })

    return poster


def upsert_poster(kon, poster: list[dict]) -> int:
    """Upsert:ar poster till publicera_kb.artikel. Returnerar antal upsertade."""
    if not poster:
        return 0

    import psycopg2.extras

    with kon:
        with kon.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                """
                INSERT INTO publicera_kb.artikel (
                    oai_id, spec, artikel_url, doi, pdf_url,
                    titel, forfattare, amnesord, abstrakt,
                    publicerad, tidskrift_namn, volym_nummer, issn,
                    sprak, licens, datestamp
                ) VALUES (
                    %(oai_id)s, %(spec)s, %(artikel_url)s, %(doi)s, %(pdf_url)s,
                    %(titel)s, %(forfattare)s, %(amnesord)s, %(abstrakt)s,
                    %(publicerad)s, %(tidskrift_namn)s, %(volym_nummer)s, %(issn)s,
                    %(sprak)s, %(licens)s, %(datestamp)s
                )
                ON CONFLICT (oai_id) DO UPDATE SET
                    artikel_url   = EXCLUDED.artikel_url,
                    doi           = EXCLUDED.doi,
                    pdf_url       = EXCLUDED.pdf_url,
                    titel         = EXCLUDED.titel,
                    forfattare    = EXCLUDED.forfattare,
                    amnesord      = EXCLUDED.amnesord,
                    abstrakt      = EXCLUDED.abstrakt,
                    publicerad    = EXCLUDED.publicerad,
                    tidskrift_namn = EXCLUDED.tidskrift_namn,
                    volym_nummer  = EXCLUDED.volym_nummer,
                    issn          = EXCLUDED.issn,
                    sprak         = EXCLUDED.sprak,
                    licens        = EXCLUDED.licens,
                    datestamp     = EXCLUDED.datestamp
                """,
                poster,
                page_size=100,
            )
            # fts_tsv är en generated column (GENERATED ALWAYS AS) —
            # PostgreSQL underhåller den automatiskt vid varje upsert.

    return len(poster)


# --- Synk-logik --------------------------------------------------------------

def synka_tidskrift(kon, spec: str, namn: str, fran_datum: str | None) -> int:
    """
    Synkar metadata för en tidskrift. Returnerar antal upsertade poster.
    fran_datum: ISO-datum (YYYY-MM-DD) för inkrementell synk, eller None för full.
    """
    bas_url = f"https://publicera.kb.se/{spec}/oai?verb=ListRecords&metadataPrefix=oai_dc"
    if fran_datum:
        url = f"{bas_url}&from={fran_datum}"
        print(f"  Inkrementell synk från {fran_datum}")
    else:
        url = bas_url
        print(f"  Full synk")

    totalt = 0
    sida = 1

    while url:
        try:
            with urllib.request.urlopen(url, timeout=PUBLICERA_TIMEOUT) as resp:
                root = ET.fromstring(resp.read())
        except Exception as fel:
            print(f"  FEL sida {sida}: {fel}")
            break

        # Kontrollera OAI-PMH-fel (t.ex. noRecordsMatch)
        fel_elem = root.find(f".//{{{NS_OAI}}}error")
        if fel_elem is not None:
            kod = fel_elem.get("code", "")
            if kod == "noRecordsMatch":
                print(f"  Inga nya poster sedan {fran_datum}")
            else:
                print(f"  OAI-PMH-fel: {kod} — {fel_elem.text}")
            break

        records = root.findall(f".//{{{NS_OAI}}}record")
        if not records:
            break

        # Sätt spec om den saknas i poster (kan hända vid per-tidskrift-endpoint)
        for rec in records:
            header = rec.find(f"{{{NS_OAI}}}header")
            if header is not None:
                befintliga = [e.text for e in header.findall(f"{{{NS_OAI}}}setSpec")]
                if not any(":" not in (t or "") for t in befintliga):
                    ny = ET.SubElement(header, f"{{{NS_OAI}}}setSpec")
                    ny.text = spec

        poster = tolka_poster(records)
        antal = upsert_poster(kon, poster)
        totalt += antal

        token_elem = root.find(f".//{{{NS_OAI}}}resumptionToken")
        csize = token_elem.get("completeListSize") if token_elem is not None else None
        csize_str = f"/{csize}" if csize else ""
        print(f"  Sida {sida}: {antal} poster (totalt {totalt}{csize_str})")

        if token_elem is not None and token_elem.text and token_elem.text.strip():
            url = (
                f"https://publicera.kb.se/{spec}/oai"
                f"?verb=ListRecords&resumptionToken={token_elem.text.strip()}"
            )
            sida += 1
            time.sleep(PUBLICERA_PAUS)
        else:
            url = None

    return totalt


# --- Schemaläggning ----------------------------------------------------------

def installera_schema(script_sokvag: str) -> None:
    """Installerar dagligt schemalagt synk-jobb (launchd eller cron).

    Styrs av SCHEMALAGGARE i .env:
      launchd  — macOS-nativt, körs även efter viloläge (rekommenderas på Mac)
      cron     — fungerar på Linux och macOS

    Tidpunkt styrs av CRON_SCHEMA i .env (standard: 03:30 varje natt).
    Python-sökväg styrs av PYTHON_SOKVAG i .env (standard: ../.venv/bin/python3
    relativt skriptmappen, dvs. ~/MCP-Servers/.venv/bin/python3).
    """
    schemalaggare = os.getenv("SCHEMALAGGARE", "launchd").lower()
    cron_schema   = os.getenv("CRON_SCHEMA", "30 3 * * *")
    script_abs    = str(Path(script_sokvag).resolve())
    skript_mapp   = Path(script_sokvag).parent.resolve()

    # Bygg absolut Python-sökväg — standard: den gemensamma venv i MCP-Servers
    python_rel = os.getenv("PYTHON_SOKVAG", "../.venv/bin/python3")
    if not os.path.isabs(python_rel):
        python_abs = str((skript_mapp / python_rel).resolve())
    else:
        python_abs = python_rel

    if schemalaggare == "launchd":
        if platform.system() != "Darwin":
            log.error("launchd är bara tillgängligt på macOS. Byt SCHEMALAGGARE=cron i .env.")
            return

        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_fil = plist_dir / "se.magnuskolsjo.mcp-publicera-kb-synk.plist"
        plist_dir.mkdir(parents=True, exist_ok=True)

        delar  = cron_schema.split()
        minut, timme = delar[0], delar[1]

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>se.magnuskolsjo.mcp-publicera-kb-synk</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_abs}</string>
        <string>{script_abs}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{timme}</integer>
        <key>Minute</key>
        <integer>{minut}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{Path.home()}/Library/Logs/publicera-kb-synk.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/Library/Logs/publicera-kb-synk-fel.log</string>
</dict>
</plist>"""

        with open(plist_fil, "w") as fh:
            fh.write(plist)

        # Avlasta eventuellt existerande jobb innan inlastning — idempotent.
        # launchctl unload misslyckas tyst om jobbet inte är registrerat.
        subprocess.run(
            ["launchctl", "unload", str(plist_fil)],
            capture_output=True,   # Ignorera fel (jobbet kan vara oregistrerat)
        )
        subprocess.run(["launchctl", "load", str(plist_fil)], check=True)
        log.info("launchd-jobb installerat: %s", plist_fil)
        log.info("Kör dagligen kl. %s:%s. Loggar: ~/Library/Logs/", timme, minut)

    else:
        # cron — fungerar på Linux och macOS
        rad = f"{cron_schema} {python_abs} {script_abs}\n"
        befintlig = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout

        if script_abs in befintlig:
            log.info("Cron-jobb finns redan. Ingen ändring gjord.")
            return

        ny_crontab = befintlig + rad
        proc = subprocess.run(["crontab", "-"], input=ny_crontab, text=True)
        if proc.returncode == 0:
            log.info("Cron-jobb tillagt: %s", rad.strip())
        else:
            log.error("Kunde inte uppdatera crontab.")


# --- Huvud -------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synkar metadata från KB Publicera till PostgreSQL via OAI-PMH"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Tvinga full omsynk (ignorera senaste synkdatum)"
    )
    parser.add_argument(
        "--tidskrift", metavar="SPEC",
        help="Synka bara en specifik tidskrift (t.ex. sjpa)"
    )
    parser.add_argument(
        "--installera-schema", action="store_true",
        help=(
            "Installera dagligt schemalagt jobb (launchd på macOS, cron på Linux). "
            "Styrs av SCHEMALAGGARE och CRON_SCHEMA i .env."
        ),
    )
    args = parser.parse_args()

    if args.installera_schema:
        installera_schema(__file__)
        return

    print("=" * 65)
    print("Synkar KB Publicera-metadata till PostgreSQL")
    print("=" * 65)

    kon = db_anslut()
    skapa_schema(kon)
    upsert_tidskrifter(kon)

    # Rensa artiklar från avdelningar som tagits bort ur PUBLICERA_DDK_FILTER
    borttagna = rensa_utgangna_tidskrifter(kon, DDK_FILTER)
    if borttagna > 0:
        print(f"Cache-rensning: {borttagna} artiklar borttagna (avdelning ej längre i DDK_FILTER)\n")

    # Välj vilka tidskrifter att synka
    if args.tidskrift:
        att_synka = [(t[0], t[1]) for t in RELEVANTA_TIDSKRIFTER if t[0] == args.tidskrift]
        if not att_synka:
            # Tillåt synk av valfri tidskrift om den anges explicit
            att_synka = [(args.tidskrift, args.tidskrift)]
    elif TIDSKRIFTER_FILTER:
        att_synka = [(t[0], t[1]) for t in RELEVANTA_TIDSKRIFTER if t[0] in TIDSKRIFTER_FILTER]
    else:
        att_synka = [(t[0], t[1]) for t in RELEVANTA_TIDSKRIFTER]

    print(f"\nSynkar {len(att_synka)} tidskrift(er)\n")

    totalt_alla = 0
    for spec, namn in att_synka:
        print(f"[{spec}] {namn}")

        fran_datum = None if args.force else hamta_senast_synkad(kon, spec)
        antal = synka_tidskrift(kon, spec, namn, fran_datum)
        uppdatera_sync_status(kon, spec, antal)
        totalt_alla += antal
        print(f"  → {antal} poster klara\n")
        time.sleep(PUBLICERA_PAUS)

    kon.close()
    print(f"Synk klar. Totalt {totalt_alla} poster upsertade.")


if __name__ == "__main__":
    main()
