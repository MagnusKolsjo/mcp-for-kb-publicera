#!/usr/bin/env python3
"""
KB Publicera MCP-server

Ger Claude tillgång till öppet tillgängliga svenska vetenskapliga tidskrifter
via OAI-PMH från KB:s Publicera-plattform.

Verktyg:
    publicera_lista_tidskrifter  — översikt över indexerade tidskrifter
    publicera_sok                — fritextsökning i cachad data (titel + abstrakt)
    publicera_hamta_artikel      — hämtar fulltext on demand (XML > HTML > EPUB > PDF)

Konfiguration: se config.example.env
Transport: stdio (för Claude Desktop)
"""

import asyncio
import contextlib
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "")
_ddk_ravar = os.getenv("PUBLICERA_DDK_FILTER", "")
DDK_FILTER: list[str] = [c.strip() for c in _ddk_ravar.split(",") if c.strip()]

PUBLICERA_BASE = "https://publicera.kb.se"
HTTP_TIMEOUT = int(os.getenv("PUBLICERA_TIMEOUT", "30"))
HTTP_PAUS = float(os.getenv("PUBLICERA_PAUS_SEKUNDER", "0.5"))

# Query-expansion (valfritt)
QUERY_EXPANSION_ENABLED     = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_BASE_URL    = os.getenv("QUERY_EXPANSION_BASE_URL",  "")
QUERY_EXPANSION_API_KEY     = os.getenv("QUERY_EXPANSION_API_KEY",   "")
QUERY_EXPANSION_MODEL       = os.getenv("QUERY_EXPANSION_MODEL",     "")
QUERY_EXPANSION_PROMPT_FILE = os.getenv(
    "QUERY_EXPANSION_PROMPT_FILE",
    str(_SCRIPT_DIR / "prompts" / "expansion_prompt.txt"),
)

# Prioriteringsordning: bäst för textextraktion först
FORMAT_PRIORITET = [
    "application/xml",
    "text/xml",
    "text/html",
    "application/epub+zip",
    "application/pdf",
]

NS_OAI = "http://www.openarchives.org/OAI/2.0/"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_OAI_DC = "http://www.openarchives.org/OAI/2.0/oai_dc/"

# ---------------------------------------------------------------------------
# Databashjälpare
# ---------------------------------------------------------------------------

_ARRAY_KOLUMNER = {"forfattare", "amnesord"}


def _ar_postgres() -> bool:
    return DATABASE_URL.startswith("postgresql")


def _hamta_db():
    """Öppnar databasanslutning — PostgreSQL eller SQLite beroende på DATABASE_URL."""
    if _ar_postgres():
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        import sqlite3
        db_fil = DATABASE_URL.replace("sqlite:///", "") or "publicera_kb_cache.db"
        if not os.path.isabs(db_fil):
            db_fil = str(_SCRIPT_DIR / db_fil)
        conn = sqlite3.connect(db_fil)
        conn.row_factory = sqlite3.Row
        return conn


@contextlib.contextmanager
def _cursor(conn):
    """Kontexthanterare för databascursor (PostgreSQL och SQLite)."""
    if _ar_postgres():
        with conn.cursor() as cur:
            yield cur
    else:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


def _prefix(tabell: str) -> str:
    """Returnerar fullt kvalificerat tabellnamn (med schema-prefix för PostgreSQL)."""
    return f"publicera_kb.{tabell}" if _ar_postgres() else tabell


def _till_db_lista(lista: list) -> object:
    """Serialiserar en lista för databas-insert: list för PG, JSON-sträng för SQLite."""
    if _ar_postgres():
        return lista or []
    return json.dumps(lista or [], ensure_ascii=False)


def _normalisera_rad(row) -> dict | None:
    """Konverterar en databasrad till dict med rätt typer (listor för array-kolumner)."""
    if row is None:
        return None
    d = dict(row)
    if not _ar_postgres():
        for kol in _ARRAY_KOLUMNER:
            if kol in d and isinstance(d[kol], str):
                try:
                    d[kol] = json.loads(d[kol]) if d[kol] else []
                except (json.JSONDecodeError, TypeError):
                    d[kol] = []
    return d


import logging
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")


def expandera_fraga(query: str) -> list[str]:
    """
    Expandera söktermen med flerspråkiga ekvivalenter via ett valfritt LLM-anrop.

    Returnerar en lista med kompletterande söktermer på svenska, engelska,
    franska, tyska, bokmål, nynorsk och danska — eller tom lista om
    query-expansion är inaktiverat eller misslyckas.

    Aktiveras via QUERY_EXPANSION_ENABLED=true i .env. Stöder alla
    OpenAI-kompatibla endpoints — konfigurera QUERY_EXPANSION_BASE_URL,
    QUERY_EXPANSION_API_KEY och QUERY_EXPANSION_MODEL.

    Promptfilen (prompts/expansion_prompt.txt) kan redigeras fritt för att
    anpassa expansionen till specifikt material eller ämnesområde.
    """
    if not QUERY_EXPANSION_ENABLED:
        return []

    prompt_path = Path(QUERY_EXPANSION_PROMPT_FILE)
    if not prompt_path.exists():
        log.warning("Promptfil för query-expansion saknas: %s", prompt_path)
        return []

    try:
        from openai import OpenAI

        prompt_template = prompt_path.read_text(encoding="utf-8")
        prompt = prompt_template.format(query=query)

        client = OpenAI(
            base_url=QUERY_EXPANSION_BASE_URL or None,
            api_key=QUERY_EXPANSION_API_KEY or "placeholder",
        )
        response = client.chat.completions.create(
            model=QUERY_EXPANSION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        raw   = response.choices[0].message.content.strip()
        terms = [t.strip() for t in raw.split(",") if t.strip()]
        log.info("Query-expansion: %r → %s", query, terms)
        return terms[:10]

    except Exception as exc:
        log.warning("Query-expansion misslyckades (fortsätter utan): %s", exc)
        return []


def ensure_schema(conn) -> None:
    with _cursor(conn) as cur:
        if _ar_postgres():
            cur.execute("CREATE SCHEMA IF NOT EXISTS publicera_kb")
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
                ALTER TABLE publicera_kb.artikel
                    ADD COLUMN IF NOT EXISTS fts_tsv TSVECTOR
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS artikel_fts_idx
                    ON publicera_kb.artikel USING GIN (fts_tsv)
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
                CREATE TABLE IF NOT EXISTS publicera_kb.sync_status (
                    spec          TEXT PRIMARY KEY,
                    senast_synkad TIMESTAMPTZ,
                    antal_poster  INTEGER DEFAULT 0,
                    status        TEXT DEFAULT 'klar'
                )
            """)
        else:
            # SQLite — enklare schema utan prefix, TEXT för arrayer och tidsstämplar
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tidskrift (
                    spec          TEXT PRIMARY KEY,
                    namn          TEXT NOT NULL,
                    ddk_avdelning TEXT,
                    ddk_kod       TEXT,
                    sao_amnesord  TEXT,
                    antal_poster  INTEGER,
                    synkad        INTEGER DEFAULT 0,
                    uppdaterad    TEXT DEFAULT (datetime('now'))
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS artikel (
                    oai_id          TEXT PRIMARY KEY,
                    spec            TEXT NOT NULL,
                    artikel_url     TEXT,
                    doi             TEXT,
                    pdf_url         TEXT,
                    titel           TEXT,
                    forfattare      TEXT,
                    amnesord        TEXT,
                    abstrakt        TEXT,
                    publicerad      TEXT,
                    tidskrift_namn  TEXT,
                    volym_nummer    TEXT,
                    issn            TEXT,
                    sprak           TEXT,
                    licens          TEXT,
                    datestamp       TEXT,
                    fulltext        TEXT,
                    fulltext_format TEXT,
                    fulltext_cachad TEXT,
                    indexerad       TEXT DEFAULT (datetime('now'))
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS artikel_spec_idx ON artikel (spec)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS artikel_publicerad_idx
                    ON artikel (publicerad DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sync_status (
                    spec          TEXT PRIMARY KEY,
                    senast_synkad TEXT,
                    antal_poster  INTEGER DEFAULT 0,
                    status        TEXT DEFAULT 'klar'
                )
            """)
    conn.commit()


# ---------------------------------------------------------------------------
# OAI-PMH hjälpare
# ---------------------------------------------------------------------------


def _text(elem, tag: str, ns: str = NS_OAI) -> str:
    child = elem.find(f"{{{ns}}}{tag}")
    return child.text.strip() if child is not None and child.text else ""


def _falt(meta, tag: str) -> list[str]:
    return [e.text.strip() for e in meta.findall(f"{{{NS_DC}}}{tag}") if e.text]


def _tolka_post(record) -> dict | None:
    header = record.find(f"{{{NS_OAI}}}header")
    if header is None or header.get("status") == "deleted":
        return None

    oai_id = _text(header, "identifier")
    set_specs = [
        s.text for s in header.findall(f"{{{NS_OAI}}}setSpec")
        if s.text and ":" not in s.text
    ]
    spec = set_specs[0] if set_specs else ""

    meta = record.find(f".//{{{NS_OAI_DC}}}dc")
    if meta is None:
        return None

    identifiers = _falt(meta, "identifier")
    datum_lista = _falt(meta, "date")
    beskrivningar = _falt(meta, "description")

    url = next((i for i in identifiers if i.startswith("http")), None)
    doi = next((i for i in identifiers if i.startswith("10.")), None)

    # Datum: föredra YYYY-MM-DD, acceptera YYYY
    publicerad = None
    for d in datum_lista:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", d)
        if m:
            publicerad = m.group(1)
            break
        m2 = re.match(r"(\d{4})", d)
        if m2:
            publicerad = f"{m2.group(1)}-01-01"
            break

    titlar = _falt(meta, "title")

    return {
        "oai_id":      oai_id,
        "spec":        spec,
        "titel":       titlar[0] if titlar else None,
        "forfattare":  _falt(meta, "creator"),
        "publicerad":  publicerad,
        "abstrakt":    beskrivningar[0] if beskrivningar else None,
        "sprak":       (_falt(meta, "language") or [None])[0],
        "doi":         doi,
        "artikel_url": url,
        "amnesord":    _falt(meta, "subject"),
    }


def hamta_artikel_fran_oai(spec: str, oai_id: str) -> dict | None:
    url = (
        f"{PUBLICERA_BASE}/{spec}/oai"
        f"?verb=GetRecord&metadataPrefix=oai_dc&identifier={oai_id}"
    )
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            root = ET.fromstring(resp.read())
        records = root.findall(f".//{{{NS_OAI}}}record")
        return _tolka_post(records[0]) if records else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Galley-hämtning och format-konvertering
# ---------------------------------------------------------------------------


def _http_get(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "KB-Publicera-MCP/1.0"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception:
        return None


def _http_head_content_type(url: str) -> str | None:
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "KB-Publicera-MCP/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ct = resp.headers.get("Content-Type", "")
            return ct.split(";")[0].strip() or None
    except Exception:
        return None


def hamta_galley_urls(landningssida_url: str) -> dict[str, str]:
    """
    Hämtar galley-URL:er från artikelns landningssida.
    OJS exponerar galleys som <a class="obj_galley_link ..."> i HTML:en.
    Returnerar {content_type: download_url}.
    """
    content = _http_get(landningssida_url)
    if not content:
        return {}

    html = content.decode("utf-8", errors="replace")

    # Primär: obj_galley_link-mönster
    hrefs = re.findall(
        r'class="obj_galley_link[^"]*"[^>]*href="([^"]+/article/view/[^"]+)"',
        html,
    )
    # Fallback: citation_pdf_url i meta-taggar
    if not hrefs:
        hrefs = re.findall(r'citation_pdf_url[^"]*content="([^"]+)"', html)

    galleys: dict[str, str] = {}
    for href in hrefs:
        download_url = href.replace("/article/view/", "/article/download/")
        ct = _http_head_content_type(download_url)
        if ct and ct not in galleys:
            galleys[ct] = download_url
        time.sleep(0.2)

    return galleys


def valj_basta_format(galleys: dict[str, str]) -> tuple[str, str] | None:
    """Väljer bästa format enligt prioriteringsordningen."""
    for fmt in FORMAT_PRIORITET:
        if fmt in galleys:
            return fmt, galleys[fmt]
    return None


def extrahera_text(content: bytes, content_type: str) -> str:
    """Konverterar nedladdat innehåll till ren klartext."""

    if content_type in ("application/xml", "text/xml"):
        try:
            root = ET.fromstring(content)
            delar = []
            for elem in root.iter():
                if elem.text and elem.text.strip():
                    delar.append(elem.text.strip())
                if elem.tail and elem.tail.strip():
                    delar.append(elem.tail.strip())
            return " ".join(delar)
        except Exception:
            return content.decode("utf-8", errors="replace")

    if content_type == "text/html":
        from html.parser import HTMLParser

        class _TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.delar: list[str] = []
                self._hoppa = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "header", "footer"):
                    self._hoppa = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "header", "footer"):
                    self._hoppa = False

            def handle_data(self, data):
                if not self._hoppa and data.strip():
                    self.delar.append(data.strip())

        parser = _TextExtractor()
        parser.feed(content.decode("utf-8", errors="replace"))
        return " ".join(parser.delar)

    if content_type == "application/epub+zip":
        try:
            delar = []
            with zipfile.ZipFile(BytesIO(content)) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith((".html", ".xhtml", ".htm")):
                        raw = zf.read(name).decode("utf-8", errors="replace")
                        text = re.sub(r"<[^>]+>", " ", raw)
                        text = re.sub(r"\s+", " ", text).strip()
                        if text:
                            delar.append(text)
            return " ".join(delar)
        except Exception:
            return ""

    if content_type == "application/pdf":
        try:
            import pdfplumber
            delar = []
            with pdfplumber.open(BytesIO(content)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        delar.append(t)
            return " ".join(delar)
        except Exception:
            return ""

    return content.decode("utf-8", errors="replace")


def hamta_fulltext(artikel: dict) -> tuple[str, str] | None:
    """
    Hämtar, konverterar och returnerar (text, content_type) för en artikel.
    Väljer bästa tillgängliga format automatiskt.
    """
    url = artikel.get("artikel_url")
    if not url:
        return None
    galleys = hamta_galley_urls(url)
    if not galleys:
        return None
    valt = valj_basta_format(galleys)
    if not valt:
        return None
    content_type, download_url = valt
    content = _http_get(download_url)
    if not content:
        return None
    text = extrahera_text(content, content_type)
    return (text, content_type) if text.strip() else None


# ---------------------------------------------------------------------------
# Cache-hjälpare
# ---------------------------------------------------------------------------


def spara_artikel(conn, artikel: dict) -> None:
    forfattare = _till_db_lista(artikel.get("forfattare") or [])
    amnesord   = _till_db_lista(artikel.get("amnesord") or [])
    if _ar_postgres():
        with _cursor(conn) as cur:
            cur.execute("""
                INSERT INTO publicera_kb.artikel
                    (oai_id, spec, artikel_url, doi, titel, forfattare,
                     amnesord, abstrakt, publicerad, sprak, indexerad)
                VALUES
                    (%(oai_id)s, %(spec)s, %(artikel_url)s, %(doi)s, %(titel)s,
                     %(forfattare)s, %(amnesord)s, %(abstrakt)s, %(publicerad)s,
                     %(sprak)s, NOW())
                ON CONFLICT (oai_id) DO UPDATE SET
                    artikel_url = EXCLUDED.artikel_url,
                    doi         = EXCLUDED.doi,
                    titel       = EXCLUDED.titel,
                    forfattare  = EXCLUDED.forfattare,
                    amnesord    = EXCLUDED.amnesord,
                    abstrakt    = EXCLUDED.abstrakt,
                    publicerad  = EXCLUDED.publicerad,
                    sprak       = EXCLUDED.sprak,
                    indexerad   = NOW()
            """, {**artikel, "forfattare": forfattare, "amnesord": amnesord})
    else:
        with _cursor(conn) as cur:
            cur.execute("""
                INSERT INTO artikel
                    (oai_id, spec, artikel_url, doi, titel, forfattare,
                     amnesord, abstrakt, publicerad, sprak, indexerad)
                VALUES
                    (:oai_id, :spec, :artikel_url, :doi, :titel, :forfattare,
                     :amnesord, :abstrakt, :publicerad, :sprak, datetime('now'))
                ON CONFLICT (oai_id) DO UPDATE SET
                    artikel_url = excluded.artikel_url,
                    doi         = excluded.doi,
                    titel       = excluded.titel,
                    forfattare  = excluded.forfattare,
                    amnesord    = excluded.amnesord,
                    abstrakt    = excluded.abstrakt,
                    publicerad  = excluded.publicerad,
                    sprak       = excluded.sprak,
                    indexerad   = datetime('now')
            """, {**artikel, "forfattare": forfattare, "amnesord": amnesord})
    conn.commit()


def spara_fulltext(conn, oai_id: str, text: str, content_type: str) -> None:
    tabell = _prefix("artikel")
    if _ar_postgres():
        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE {tabell} SET fulltext=%s, fulltext_format=%s,"
                " fulltext_cachad=NOW() WHERE oai_id=%s",
                (text, content_type, oai_id),
            )
    else:
        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE {tabell} SET fulltext=?, fulltext_format=?,"
                " fulltext_cachad=datetime('now') WHERE oai_id=?",
                (text, content_type, oai_id),
            )
    conn.commit()


def hamta_cachad_artikel(conn, oai_id: str) -> dict | None:
    tabell = _prefix("artikel")
    ph = "%s" if _ar_postgres() else "?"
    with _cursor(conn) as cur:
        cur.execute(f"SELECT * FROM {tabell} WHERE oai_id = {ph}", (oai_id,))
        row = cur.fetchone()
    return _normalisera_rad(row)


# ---------------------------------------------------------------------------
# MCP-server och verktyg
# ---------------------------------------------------------------------------

server = Server("publicera-kb-v4")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="publicera_lista_tidskrifter",
            description=(
                "Listar alla indexerade tidskrifter på KB Publicera med DDK-avdelning "
                "och antal cachade artiklar. Använd för att ge en källöversikt eller "
                "för att hitta rätt tidskriftskod (spec) inför en artikelsökning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ddc_filter": {
                        "type": "string",
                        "description": (
                            "Filtrera på DDK-avdelning, t.ex. '340' för juridik. "
                            "Lämna tomt för alla konfigurerade avdelningar."
                        ),
                    }
                },
            },
        ),
        Tool(
            name="publicera_sok",
            description=(
                "Sökning i titlar och abstrakt för artiklar på KB Publicera. "
                "Söker i all cachad data. Returnerar träffar med "
                "titel, författare, år, tidskrift och abstrakt."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sokterm": {
                        "type": "string",
                        "description": (
                            "Sökterm eller kommaseparerad lista av termer. "
                            "Varje kommaavdelad term matchas med OR-logik — "
                            "t.ex. 'rättssäkerhet, legal certainty, Rechtssicherheit' "
                            "hittar artiklar som innehåller någon av termerna. "
                            "Ord inom en term matchas med AND."
                        ),
                    },
                    "ddk_avdelning": {
                        "type": "string",
                        "description": (
                            "Begränsa till en eller flera DDK-avdelningar, "
                            "kommaseparerade, t.ex. '340,350' eller '900'. "
                            "Lämna tomt för att söka i alla avdelningar."
                        ),
                    },
                    "max_resultat": {
                        "type": "integer",
                        "description": "Maximalt antal resultat (standard: 10, max: 50).",
                        "default": 10,
                    },
                },
                "required": ["sokterm"],
            },
        ),
        Tool(
            name="publicera_hamta_artikel",
            description=(
                "Hämtar fullständig metadata och fulltext för en specifik artikel. "
                "Laddar ned och konverterar från bästa tillgängliga format i prioritetsordning: "
                "XML > HTML > EPUB > PDF. Resultatet cachelagras i PostgreSQL. "
                "Kräver oai_id; ange även spec om artikeln inte redan finns i cachen."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "oai_id": {
                        "type": "string",
                        "description": (
                            "Artikelns OAI-identifierare, "
                            "t.ex. 'oai:ojs.publicera.kb.se:article/17638'."
                        ),
                    },
                    "spec": {
                        "type": "string",
                        "description": (
                            "Tidskriftens spec-kod, t.ex. 'ejels' eller 'sjpa'. "
                            "Krävs om artikeln inte finns i cachen."
                        ),
                    },
                },
                "required": ["oai_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    conn = _hamta_db()
    try:
        ensure_schema(conn)
        match name:
            case "publicera_lista_tidskrifter":
                return await _lista_tidskrifter(conn, arguments)
            case "publicera_sok":
                return await _sok(conn, arguments)
            case "publicera_hamta_artikel":
                return await _hamta_artikel(conn, arguments)
            case _:
                return [TextContent(type="text", text=f"Okänt verktyg: {name}")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Verktygimplementationer
# ---------------------------------------------------------------------------


async def _lista_tidskrifter(conn, args: dict) -> list[TextContent]:
    ddc_filter = args.get("ddc_filter", "").strip() or None
    t_tab = _prefix("tidskrift")
    a_tab = _prefix("artikel")

    with _cursor(conn) as cur:
        params: list = []
        where_delar: list[str] = []

        if ddc_filter:
            where_delar.append("t.ddk_avdelning = %s" if _ar_postgres() else "t.ddk_avdelning = ?")
            params.append(ddc_filter)
        elif DDK_FILTER:
            if _ar_postgres():
                where_delar.append("t.ddk_avdelning = ANY(%s)")
                params.append(DDK_FILTER)
            else:
                placeholders = ",".join("?" * len(DDK_FILTER))
                where_delar.append(f"t.ddk_avdelning IN ({placeholders})")
                params.extend(DDK_FILTER)

        where_sql = ("WHERE " + " AND ".join(where_delar)) if where_delar else ""
        nulls = "NULLS LAST" if _ar_postgres() else ""
        cur.execute(f"""
            SELECT t.spec, t.namn, t.ddk_avdelning, t.ddk_kod,
                   COUNT(a.oai_id) AS antal_cachade
            FROM {t_tab} t
            LEFT JOIN {a_tab} a ON a.spec = t.spec
            {where_sql}
            GROUP BY t.spec, t.namn, t.ddk_avdelning, t.ddk_kod
            ORDER BY t.ddk_avdelning {nulls}, t.namn
        """, params)
        rows = cur.fetchall()

    if not rows:
        return [TextContent(type="text", text="Inga tidskrifter hittades i databasen.")]

    rader = [f"## Tidskrifter på KB Publicera ({len(rows)} st)\n"]
    for r in rows:
        avd = r["ddk_avdelning"] or "–"
        kod = f" [{r['ddk_kod']}]" if r["ddk_kod"] else ""
        rader.append(
            f"**{r['namn']}** (`{r['spec']}`)\n"
            f"  DDK {avd}{kod} | Cachade artiklar: {r['antal_cachade']}"
        )

    return [TextContent(type="text", text="\n\n".join(rader))]


async def _sok(conn, args: dict, extra_ddc: list[str] | None = None) -> list[TextContent]:
    sokterm = args.get("sokterm", "").strip()
    _ddc_arg = args.get("ddk_avdelning", "").strip()
    # Stöder kommaseparerade koder: "340,350" → ["340", "350"]
    ddk_avdelning_lista = [k.strip() for k in _ddc_arg.split(",") if k.strip()] if _ddc_arg else []
    max_resultat = min(int(args.get("max_resultat", 10)), 50)

    if not sokterm:
        return [TextContent(type="text", text="Ange en sökterm.")]

    a_tab = _prefix("artikel")
    t_tab = _prefix("tidskrift")
    aktiv_ddk = extra_ddc or (ddk_avdelning_lista if ddk_avdelning_lista else DDK_FILTER or [])

    # Query-expansion: flerspråkiga ekvivalenter via valfritt LLM-anrop
    extra_terms = expandera_fraga(sokterm)

    # Dela upp söktermen i individuella OR-grenar:
    # Kommaseparering är det enda sättet att dela upp i OR-grenar:
    # - Kommaseparerat ("rättssäkerhet, legal certainty"): varje del OR:as,
    #   ord inom en del matchas med AND ("legal certainty" → legal & certainty).
    # - Inga kommatecken: hela strängen behandlas som en enda fras/term.
    #   Splittning på mellanslag är avsiktligt UNDVIKT — det förstör fraser
    #   ("legal certainty" → "legal" OR "certainty") och ger för breda träffar
    #   i stora databaser. Skicka kommaseparerade termer för OR-logik.
    if "," in sokterm:
        sokterm_delar = [t.strip() for t in sokterm.split(",") if t.strip()]
    else:
        sokterm_delar = [sokterm.strip()]
    alla_termer = sokterm_delar + extra_terms

    with _cursor(conn) as cur:
        if _ar_postgres():
            # DDK-filter med positional params
            ddk_sql = ""
            ddk_params: list = []
            if aktiv_ddk:
                placeholders = ",".join(["%s"] * len(aktiv_ddk))
                ddk_sql = f"AND t.ddk_avdelning IN ({placeholders})"
                ddk_params = list(aktiv_ddk)

            # FTS med OR-logik över alla termer (original + expanderade):
            # plainto_tsquery('simple', t1) || plainto_tsquery('simple', t2) || ...
            # 'simple' är språkagnostisk — korpusen är blandspråkig
            # (svenska: tgv, socvet m.fl.; engelska: ejels, sjpa, siplr m.fl.)
            fts_or_delar = " || ".join(["plainto_tsquery('simple', %s)"] * len(alla_termer))
            rank_or_delar = " || ".join(["plainto_tsquery('simple', %s)"] * len(alla_termer))
            fts_params = alla_termer  # för WHERE
            rank_params = alla_termer  # för ts_rank

            cur.execute(f"""
                SELECT a.oai_id, a.spec, a.titel, a.forfattare, a.publicerad,
                       a.abstrakt, a.doi, a.artikel_url, t.ddk_avdelning,
                       ts_rank(
                           to_tsvector('simple',
                               coalesce(a.titel,'') || ' ' || coalesce(a.abstrakt,'')),
                           {rank_or_delar}
                       ) AS rank
                FROM {a_tab} a
                JOIN {t_tab} t ON t.spec = a.spec
                WHERE to_tsvector('simple',
                          coalesce(a.titel,'') || ' ' || coalesce(a.abstrakt,''))
                      @@ ({fts_or_delar})
                {ddk_sql}
                ORDER BY rank DESC
                LIMIT %s
            """, rank_params + fts_params + ddk_params + [max_resultat])
        else:
            # SQLite — enkel LIKE-sökning med OR över alla termer
            like_delar = " OR ".join(
                ["(lower(a.titel) LIKE lower(?) OR lower(a.abstrakt) LIKE lower(?))"]
                * len(alla_termer)
            )
            params_sq: list = []
            for t in alla_termer:
                params_sq.extend([f"%{t}%", f"%{t}%"])
            ddk_sql = ""
            if aktiv_ddk:
                placeholders = ",".join("?" * len(aktiv_ddk))
                ddk_sql = f"AND t.ddk_avdelning IN ({placeholders})"
                params_sq.extend(aktiv_ddk)
            params_sq.append(max_resultat)
            cur.execute(f"""
                SELECT a.oai_id, a.spec, a.titel, a.forfattare, a.publicerad,
                       a.abstrakt, a.doi, a.artikel_url, t.ddk_avdelning
                FROM {a_tab} a
                JOIN {t_tab} t ON t.spec = a.spec
                WHERE ({like_delar})
                {ddk_sql}
                ORDER BY a.publicerad DESC
                LIMIT ?
            """, params_sq)
        rows = cur.fetchall()

    if not rows:
        return [TextContent(
            type="text",
            text=f"Inga artiklar hittades för '{sokterm}' i de {len(alla_termer)} sökta termerna.",
        )]

    rubrik = f"## Sökresultat: '{sokterm}' ({len(rows)} träffar)\n"
    rader = [rubrik]
    for r in rows:
        r = _normalisera_rad(r)
        forfattare = ", ".join(r["forfattare"] or []) or "Okänd"
        ar = str(r["publicerad"] or "")[:4] or "?"
        abstrakt = (r["abstrakt"] or "")[:300]
        if len(r["abstrakt"] or "") > 300:
            abstrakt += "…"
        doi_rad = f"DOI: {r['doi']} | " if r["doi"] else ""
        url_rad = f"[Länk]({r['artikel_url']})" if r["artikel_url"] else ""

        rader.append(
            f"### {r['titel'] or 'Utan titel'}\n"
            f"*{forfattare}* ({ar}) — `{r['spec']}` | DDK {r['ddk_avdelning'] or '–'}\n"
            f"{doi_rad}{url_rad}\n\n"
            f"{abstrakt}\n\n"
            f"`{r['oai_id']}`"
        )

    return [TextContent(type="text", text="\n\n---\n\n".join(rader))]


async def _hamta_artikel(conn, args: dict) -> list[TextContent]:
    oai_id = args.get("oai_id", "").strip()
    spec = args.get("spec", "").strip()

    if not oai_id:
        return [TextContent(type="text", text="Ange ett oai_id.")]

    # 1. Kolla cache
    artikel = hamta_cachad_artikel(conn, oai_id)

    # 2. Hämta från OAI-PMH om artikeln saknas
    if not artikel:
        if not spec:
            return [TextContent(
                type="text",
                text=(
                    f"Artikeln `{oai_id}` finns inte i cachen. "
                    "Ange `spec` (tidskriftskoden, t.ex. 'ejels') för att hämta den."
                ),
            )]
        ny = hamta_artikel_fran_oai(spec, oai_id)
        if not ny:
            return [TextContent(
                type="text",
                text=f"Kunde inte hämta `{oai_id}` från OAI-PMH för tidskrift '{spec}'.",
            )]
        if not ny.get("spec"):
            ny["spec"] = spec
        spara_artikel(conn, ny)
        artikel = hamta_cachad_artikel(conn, oai_id) or ny

    # 3. Hämta fulltext om den saknas
    fulltext = artikel.get("fulltext")
    fulltext_format = artikel.get("fulltext_format")

    if not fulltext:
        result = hamta_fulltext(artikel)
        if result:
            fulltext, fulltext_format = result
            spara_fulltext(conn, oai_id, fulltext, fulltext_format)

    # 4. Bygg svar
    forfattare = ", ".join(artikel.get("forfattare") or []) or "Okänd"
    ar = str(artikel.get("publicerad") or "")[:4] or "?"

    delar = [
        f"## {artikel.get('titel') or 'Utan titel'}",
        f"**Författare:** {forfattare}",
        f"**År:** {ar} | **Tidskrift:** `{artikel.get('spec', '–')}`",
    ]
    if artikel.get("doi"):
        delar.append(f"**DOI:** {artikel['doi']}")
    if artikel.get("artikel_url"):
        delar.append(f"**URL:** {artikel['artikel_url']}")
    if fulltext_format:
        delar.append(f"**Hämtat som:** {fulltext_format}")
    if artikel.get("amnesord"):
        delar.append(f"**Ämnesord:** {', '.join(artikel['amnesord'])}")
    if artikel.get("abstrakt"):
        delar.append(f"\n### Abstrakt\n{artikel['abstrakt']}")

    if fulltext:
        visning = fulltext[:8_000]
        if len(fulltext) > 8_000:
            visning += f"\n\n*[Trunkerad — {len(fulltext):,} tecken totalt]*"
        delar.append(f"\n### Fulltext\n{visning}")
    else:
        delar.append("\n*Fulltext kunde inte hämtas för denna artikel.*")

    return [TextContent(type="text", text="\n\n".join(delar))]



# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
