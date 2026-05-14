"""
Inventera tidskrifter på KB:s Publicera-plattform via OAI-PMH ListSets.

Hämtar alla tidskrifter, klassificerar dem efter relevans för projektet
och sparar resultatet till PostgreSQL (schema publicera_kb) samt till
en JSON-fil för manuell granskning.

Kör:
    python3 01_inventera_tidskrifter.py
    python3 01_inventera_tidskrifter.py --visa-alla
    python3 01_inventera_tidskrifter.py --bara-relevanta
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Konfiguration -----------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()

PUBLICERA_ENDPOINT = os.getenv(
    "PUBLICERA_ENDPOINT", "https://publicera.kb.se/index/oai"
)
PUBLICERA_TIMEOUT = int(os.getenv("PUBLICERA_TIMEOUT", "30"))
PUBLICERA_PAUS = float(os.getenv("PUBLICERA_PAUS_SEKUNDER", "0.5"))

NS_OAI = "http://www.openarchives.org/OAI/2.0/"

# --- Relevansklassificering --------------------------------------------------
# Tidskrifter klassificerade efter relevans för projektet (juridik,
# statsvetenskap, historia, samhällsvetenskap).
# Värde: (relevans, ämnesområde) — relevans 1=hög, 2=medel, 3=låg

RELEVANSREGISTER: dict[str, tuple[int, str]] = {
    "ejels":     (1, "Rättsvetenskap (empirisk juridik)"),
    "siplr":     (1, "Juridik (immaterialrätt)"),
    "sjpa":      (1, "Förvaltningsrätt / statsvetenskap"),
    "socvet":    (1, "Samhällsvetenskap"),
    "jdsr":      (1, "Digital samhällsvetenskap"),
    "ihs":       (1, "Historia & samhälle"),
    "fornvannen":(2, "Arkeologi / kulturhistoria"),
    "csa":       (2, "Arkeologi"),
    "arv":       (2, "Folkloristik / kulturhistoria"),
    "ethsc":     (2, "Etnologi"),
    "meta":      (2, "Historisk arkeologi"),
    "rig":       (2, "Kulturhistoria"),
    "fn":        (2, "Marinhistoria"),
    "kp":        (2, "Etnologi"),
    "smt":       (2, "Socialmedicin"),
    "tgv":       (2, "Genusvetenskap"),
    "insi":      (2, "Arkeologi"),
    "jonas":     (2, "Nordisk arkeologi"),
    "opuscula":  (2, "Antikvetenskap"),
    "shs":       (2, "Genealogi / historia"),
}


# --- OAI-PMH-funktioner ------------------------------------------------------

def hamta_element_text(elem, tag: str, ns: str = NS_OAI) -> str:
    """Returnerar texten för ett child-element, eller tom sträng."""
    child = elem.find(f"{{{ns}}}{tag}")
    return child.text.strip() if child is not None and child.text else ""


def hamta_alla_sets(endpoint: str) -> list[dict]:
    """
    Hämtar alla set från OAI-PMH ListSets med paginering via resumptionToken.
    Returnerar en lista med dicts: {spec, namn}.
    Filtrerar bort undersektioner (spec innehåller kolon).
    """
    alla = []
    url = f"{endpoint}?verb=ListSets"
    sida = 1

    while url:
        print(f"  Hämtar sida {sida}...", end=" ", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=PUBLICERA_TIMEOUT) as resp:
                root = ET.fromstring(resp.read())
        except Exception as fel:
            print(f"FEL: {fel}")
            break

        sets = root.findall(f".//{{{NS_OAI}}}set")
        for s in sets:
            spec = hamta_element_text(s, "setSpec")
            namn = hamta_element_text(s, "setName")
            if spec and ":" not in spec:   # Hoppa undersektioner
                alla.append({"spec": spec, "namn": namn})

        print(f"({len(sets)} element)")

        token_elem = root.find(f".//{{{NS_OAI}}}resumptionToken")
        if token_elem is not None and token_elem.text and token_elem.text.strip():
            url = f"{endpoint}?verb=ListSets&resumptionToken={token_elem.text.strip()}"
            sida += 1
            time.sleep(PUBLICERA_PAUS)
        else:
            url = None

    return alla


def hamta_antal_poster(spec: str, endpoint_bas: str) -> int | None:
    """
    Hämtar antalet poster i en tidskrift via ListIdentifiers.
    Returnerar completeListSize om tillgänglig, annars räknar headers.
    """
    # Enskild tidskrifts OAI-endpoint
    tidskrift_endpoint = endpoint_bas.replace("/index/oai", f"/{spec}/oai")
    if "/index/oai" not in endpoint_bas:
        tidskrift_endpoint = endpoint_bas.rstrip("/oai") + f"/{spec}/oai"

    url = (
        f"https://publicera.kb.se/{spec}/oai"
        f"?verb=ListIdentifiers&metadataPrefix=oai_dc"
    )
    try:
        with urllib.request.urlopen(url, timeout=PUBLICERA_TIMEOUT) as resp:
            root = ET.fromstring(resp.read())
        token = root.find(f".//{{{NS_OAI}}}resumptionToken")
        if token is not None and token.get("completeListSize"):
            return int(token.get("completeListSize"))
        headers = root.findall(f".//{{{NS_OAI}}}header")
        return len(headers)
    except Exception:
        return None


# --- Databaslagring ----------------------------------------------------------

def spara_till_databas(tidskrifter: list[dict]) -> None:
    """Sparar tidskriftslistan till PostgreSQL-schema publicera_kb."""
    try:
        import psycopg2
    except ImportError:
        print("  OBS: psycopg2 saknas — hoppar databaslagring.")
        return

    database_url = os.getenv("DATABASE_URL")
    if not database_url or database_url.startswith("sqlite"):
        print("  OBS: DATABASE_URL saknas eller är SQLite — hoppar databaslagring.")
        return

    try:
        kon = psycopg2.connect(database_url)
        with kon:
            with kon.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS publicera_kb")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS publicera_kb.tidskrift (
                        spec            TEXT PRIMARY KEY,
                        namn            TEXT NOT NULL,
                        relevans        INTEGER DEFAULT 3,
                        amnesomrade     TEXT,
                        antal_poster    INTEGER,
                        synkad          BOOLEAN DEFAULT FALSE,
                        uppdaterad      TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                for t in tidskrifter:
                    relevans, amne = RELEVANSREGISTER.get(t["spec"], (3, ""))
                    cur.execute("""
                        INSERT INTO publicera_kb.tidskrift
                            (spec, namn, relevans, amnesomrade, antal_poster)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (spec) DO UPDATE SET
                            namn         = EXCLUDED.namn,
                            relevans     = EXCLUDED.relevans,
                            amnesomrade  = EXCLUDED.amnesomrade,
                            antal_poster = EXCLUDED.antal_poster,
                            uppdaterad   = NOW()
                    """, (
                        t["spec"], t["namn"],
                        relevans, amne,
                        t.get("antal_poster")
                    ))
        kon.close()
        print(f"  Sparade {len(tidskrifter)} tidskrifter till publicera_kb.tidskrift")
    except Exception as fel:
        print(f"  Databasfel: {fel}")


# --- Huvud -------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inventera tidskrifter på KB Publicera via OAI-PMH ListSets"
    )
    parser.add_argument(
        "--visa-alla", action="store_true",
        help="Visa alla tidskrifter, inte bara relevanta"
    )
    parser.add_argument(
        "--bara-relevanta", action="store_true",
        help="Hämta bara poster för relevanta tidskrifter (relevans 1-2)"
    )
    parser.add_argument(
        "--rakna-poster", action="store_true",
        help="Hämta antal poster per tidskrift (tar tid)"
    )
    parser.add_argument(
        "--spara-db", action="store_true",
        help="Spara resultatet till PostgreSQL"
    )
    args = parser.parse_args()

    print("=" * 65)
    print("Inventerar tidskrifter på KB Publicera")
    print("=" * 65)

    print(f"\n1. Hämtar alla sets från {PUBLICERA_ENDPOINT} ...")
    tidskrifter = hamta_alla_sets(PUBLICERA_ENDPOINT)
    print(f"   Hittade {len(tidskrifter)} top-level tidskrifter\n")

    # Slå upp relevans och ämnesområde
    for t in tidskrifter:
        relevans, amne = RELEVANSREGISTER.get(t["spec"], (3, ""))
        t["relevans"] = relevans
        t["amnesomrade"] = amne
        t["antal_poster"] = None

    # Eventuellt räkna poster
    if args.rakna_poster:
        print("2. Räknar poster per tidskrift ...")
        relevanta = [t for t in tidskrifter if t["relevans"] <= 2] \
            if args.bara_relevanta else tidskrifter
        for t in relevanta:
            antal = hamta_antal_poster(t["spec"], PUBLICERA_ENDPOINT)
            t["antal_poster"] = antal
            status = str(antal) if antal is not None else "?"
            print(f"   {t['spec']:<20} {status}")
            time.sleep(PUBLICERA_PAUS)
        print()

    # Visa tabell
    visa = tidskrifter if args.visa_alla else [
        t for t in tidskrifter if t["relevans"] <= 2
    ]
    visa = sorted(visa, key=lambda x: (x["relevans"], x["namn"].lower()))

    print(f"{'#':<3} {'Spec':<15} {'Rel':<5} {'Poster':<8} {'Namn'}")
    print("-" * 80)
    for i, t in enumerate(visa, 1):
        poster = str(t["antal_poster"]) if t["antal_poster"] is not None else "-"
        rel_str = ["★★★", "★★ ", "★  "][min(t["relevans"] - 1, 2)]
        print(f"{i:<3} {t['spec']:<15} {rel_str} {poster:<8} {t['namn']}")
        if t["amnesomrade"]:
            print(f"    {'':15}      {'':8} ↳ {t['amnesomrade']}")

    # Spara JSON
    json_fil = _SCRIPT_DIR / "tidskrifter.json"
    with open(json_fil, "w", encoding="utf-8") as f:
        json.dump(tidskrifter, f, ensure_ascii=False, indent=2)
    print(f"\nSparade full lista till: {json_fil}")

    # Eventuellt spara till databas
    if args.spara_db:
        print("\n3. Sparar till PostgreSQL ...")
        spara_till_databas(tidskrifter)

    print("\nKlart.")


if __name__ == "__main__":
    main()
