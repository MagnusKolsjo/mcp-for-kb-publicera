# KB Publicera MCP-server

MCP-server för öppet tillgängliga svenska vetenskapliga tidskrifter publicerade på [KB Publicera](https://publicera.kb.se). Servern hämtar artikelmetadata via OAI-PMH-protokollet och gör innehållet sökbart via Claude.

## Vad servern gör

Servern exponerar tre verktyg:

- **publicera_lista_tidskrifter** — listar alla indexerade tidskrifter med DDK-avdelning och antal artiklar
- **publicera_sok** — fritextsökning i titlar och abstrakt för cachade artiklar
- **publicera_hamta_artikel** — hämtar fullständig metadata och fulltext för en specifik artikel on demand (XML > HTML > EPUB > PDF)

## Förutsättningar

- Python 3.11 eller senare
- PostgreSQL 14 eller senare (med pgvector om vektorsökning önskas) eller SQLite 3.35+
- Nätverksåtkomst till `publicera.kb.se`

## Installation

```bash
pip install -r requirements.txt
cp config.example.env .env
# Redigera .env med dina inställningar
python3 01_inventera_tidskrifter.py --spara-db
python3 02_synka_metadata.py
python3 mcp_server.py
```

## Konfiguration

Kopiera `config.example.env` till `.env` och anpassa:

```env
# Databasanslutning
DATABASE_URL=postgresql://localhost/riksdag

# DDK-filter: kommaseparerad lista med avdelningskoder (XX0-nivå).
# Lämna tom för att indexera alla tidskrifter.
# Exempel nedan täcker samhällsvetenskap, juridik, historia och angränsande områden.
PUBLICERA_DDK_FILTER=020,210,300,310,320,330,340,350,360,370,380,390,430,900,910,920,930,940,950,960,970,980,990
```

DDK-avdelningarna beskrivs i tabellen nedan och i [Dewey decimalklassifikation — översikt (KB, 2023)](https://metadatabyran.kb.se/download/18.44613d3618ee55a56596c26/1716212730072/dewey_oversikt_23.pdf).

> **OBS: Borttaging av avdelningar påverkar cachen.**
> Om du tar bort en DDK-avdelningskod från `PUBLICERA_DDK_FILTER` och kör `02_synka_metadata.py` igen, raderas automatiskt alla cachade artiklar som tillhör tidskrifter i den borttagna avdelningen. Ändringen är permanent — artiklarna måste hämtas om via en ny synk om avdelningen läggs tillbaka.

## Tidskrifter

53 av 55 tidskrifter på KB Publicera finns representerade nedan. Undantagen är *Sömn och Hälsa* (saknas i Libris) och *Publicera Support* (intern resurssida). DDK-avdelning hämtas från Libris; tre tidskrifter saknar officiell DDK-klassning och har tilldelats avdelning manuellt (markerade med *).

| Tidskrift | ISSN | DDK-avdelning |
|---|---|---|
| Acta Logopaedica | 2004-9048 | 610 Medicin & hälsa |
| Adoranten | 3035-9880 | 700 Konstarter |
| AGATHEOS – European Journal for Philosophy of Religion | 2004-9331 | 210 Religionsfilosofi & religionsteori |
| Arbete och Hälsa | 0346-7821 | 330 Nationalekonomi |
| Arv – Nordic Yearbook of Folklore | 2002-4185 | 390 Seder, etikett & folklore |
| ASLA:s skriftserie | 1100-5629 | 410 Språkvetenskap |
| Childhood in the Anthropocene | 2004-9811 | 300 Samhällsvetenskaper, sociologi & antropologi |
| Current Issues in Work-Integrated Learning | 3035-6903 | 370 Utbildning |
| Current Swedish Archaeology | 2002-3901 | 930 Forntida världens historia (före ca 499) |
| Educare | 2004-5190 | 370 Utbildning |
| Ethnologia Scandinavica | 2003-6043 | 300 Samhällsvetenskaper, sociologi & antropologi |
| European Journal of Empirical Legal Studies | 2004-8556 | 340 Juridik |
| European Journal of Philosophy in Arts Education | 2002-4665 | 700 Konstarter |
| Fornvännen | 1404-9430 | 940 Europas historia |
| Forskning om undervisning och lärande | 2001-6131 | 370 Utbildning |
| Forum navale | 0280-6215 | 350 Offentlig förvaltning & militärvetenskap |
| HYBRID – mellan akademi, kyrka och samhälle | 2004-5417 | 230 Kristendom & kristen teologi |
| Högskolepedagogisk debatt | 2004-3929 | 370 Utbildning |
| Idrott, historia och samhälle | 2004-7843 | 790 Idrott, spel & underhållning |
| In Situ Archaeologica | 2002-7656 | 940 Europas historia |
| Information Research | 1368-1613 | 020 Biblioteks- & informationsvetenskap |
| Journal of Digital Social Research | 2003-1998 | 300 Samhällsvetenskaper, sociologi & antropologi |
| Journal of Endovascular Resuscitation and Trauma Management | 2002-7567 | 610 Medicin & hälsa |
| Journal of Nordic Archaeological Science – JONAS | 2002-4223 | 930 Forntida världens historia (före ca 499) |
| Journal of Praxis in Higher Education | 2003-3605 | 370 Utbildning |
| Kapet | 1653-4743 | 370 Utbildning |
| Kulturella Perspektiv – Svensk etnologisk tidskrift | 2004-0288 | 300 Samhällsvetenskaper, sociologi & antropologi |
| lambda nordica | 2001-7286 | 300 Samhällsvetenskaper, sociologi & antropologi |
| META – Historiskarkeologisk tidskrift | 2002-0406 | 930 Forntida världens historia (före ca 499) |
| Moderna Språk | 2000-3560 | 400 Språk |
| Namn och bygd | 2002-4177 | 910 Geografi & resor |
| Nordic Journal of English Studies | 1654-6970 | 420 Engelska & fornengelska |
| Nordisk Tidskrift för Allmän Didaktik | 2002-1534 | 370 Utbildning |
| Nordisk tidskrift för socioonomastik | 2004-0881 | 920 Biografi & genealogi |
| Opuscula. Annual of the Swedish Institutes at Athens and Rome | 2004-7142 | 930 Forntida världens historia (före ca 499) |
| Pedagogisk forskning i Sverige | 2001-3345 | 370 Utbildning |
| Puls – musik- och dansetnologisk tidskrift | 2002-2972 | 780 Musik |
| Rig. Kulturhistorisk tidskrift | 2002-3863 | 300 Samhällsvetenskaper, sociologi & antropologi |
| Samlaren | 0036-5106 | 830 Tyska & besläktade litteraturer * |
| Scandinavian Journal of Public Administration | 2001-7413 | 350 Offentlig förvaltning & militärvetenskap |
| Scripta Islandica | 2001-9416 | 830 Tyska & besläktade litteraturer |
| Släkthistoriska Studier | 2004-3910 | 920 Biografi & genealogi |
| Snow Leopard Reports | 2004-5255 | 590 Djur (zoologi) |
| Socialmedicinsk tidskrift | 2000-4192 | 360 Sociala problem & sociala tjänster |
| Socialvetenskaplig tidskrift | 2003-5624 | 360 Sociala problem & sociala tjänster |
| Språk och stil | 2002-4010 | 430 Tyska & besläktade språk |
| Stockholm Intellectual Property Law Review | 2003-2382 | 340 Juridik |
| Svensk Exegetisk Årsbok | 2001-9424 | 260 Kristen organisation, socialt arbete & tillbedjan |
| Svensk tidskrift för musikforskning / Swedish Journal of Music Research | 1653-9672 | 780 Musik * |
| Svenska landsmål och svenskt folkliv | 2004-9242 | 430 Tyska & besläktade språk |
| Tidskrift för genusvetenskap | 2001-1377 | 300 Samhällsvetenskaper, sociologi & antropologi |
| Tidskrift för litteraturvetenskap | 0346-6469 | 800 Litteratur, retorik & analys * |
| Utbildning och Lärande | 2001-4554 | 300 Samhällsvetenskaper, sociologi & antropologi |

\* Avdelning tilldelad manuellt — tidskriften saknar DDK-klassning i Libris.

DDK-avdelningarna följer Dewey decimalklassifikation (DDK 23), som Kungliga biblioteket tillämpar sedan 2011. En fullständig översikt finns i [Dewey decimalklassifikation — översikt (KB, 2023)](https://metadatabyran.kb.se/download/18.44613d3618ee55a56596c26/1716212730072/dewey_oversikt_23.pdf).

## Databasbackend

Servern stöder PostgreSQL och SQLite som symmetriska val — inget är "standard" eller "fallback". Välj backend via `DATABASE_URL` i `.env`:

```env
# PostgreSQL
DATABASE_URL=postgresql://anvandare:losenord@localhost:5432/riksdagstryck

# SQLite
DATABASE_URL=sqlite:///publicera_kb_cache.db
```

PostgreSQL rekommenderas för produktionsanvändning eftersom det möjliggör FTS-sökning med GIN-index och `'simple'`-konfiguration för blandspråkig korpus (svenska + engelska). SQLite använder LIKE-sökning utan stemming.

## Daglig synk

Synk-skriptet hämtar nya och ändrade poster sedan senaste körning (inkrementell) och kan schemaläggas automatiskt:

```bash
# Kör manuellt
python3 02_synka_metadata.py

# Tvinga full omsynk av alla tidskrifter
python3 02_synka_metadata.py --force

# Installera dagligt schemalagt jobb (launchd på macOS, cron på Linux)
python3 02_synka_metadata.py --installera-schema
```

Tidpunkt och schemaläggare styrs av `CRON_SCHEMA` och `SCHEMALAGGARE` i `.env`.

## Licens

GNU Affero General Public License v3.0 (AGPL-3.0). Se `LICENSE`.
