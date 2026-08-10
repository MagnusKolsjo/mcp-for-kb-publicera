# Ändringslogg

Alla märkbara ändringar i detta projekt dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.0.0/).

---

## [2.1.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `publicera_hamta_artikel`** (standard 8 000
  tecken, `0` ger hela artikeln).

### Ändrat

- **Trunkeringen visar nu vägen vidare.** Fulltexten kapades tidigare vid 8 000 tecken
  med markeringen `[Trunkerad — N tecken totalt]`. Markeringen var korrekt men det fanns
  inget sätt att läsa resten — artikeln bortom teckengränsen var oåtkomlig. Markeringen
  anger nu vilket intervall som visas och det färdiga anropet för att fortsätta:
  `[Visar tecken 8 001–16 000 av 82 892. Läs vidare: publicera_hamta_artikel(oai_id="…", fran_tecken=16000)]`.
  Kapningen sker dessutom på ordgräns i stället för mitt i ett ord.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar och fält; inga brytande
ändringar och inga schemaändringar. Cachen och databasen lagrar fortfarande hela
texten — trunkeringen gäller bara svaret till anroparen, så sökning och indexering
påverkas inte.

---

## [2.0.1] — 2026-05-22

### Åtgärdat

- **Migration M1** — `array_to_string(amnesord, ' ')` orsakade `generation expression is not immutable`
  eftersom `array_to_string(anyarray, text)` är STABLE (ej IMMUTABLE) i PostgreSQL.
  Lösning: `amnesord` utesluts ur generated column-uttrycket; FTS indexerar `titel || abstrakt`.
  `'simple'::regconfig`-casten behålls (krävs för att `to_tsvector` ska godkännas som immutable).

---

---

## [2.0.0] — 2026-05-22

Första publika version. Samlar alla ändringar sedan intern prototyp.

### Bakåtbrytande ändringar

- **Servernamn ändrat** — MCP-servernamnet är nu `publicera-kb` (tidigare `publicera-kb-v4`).
  Uppdatera `mcpServers`-nyckeln i `claude_desktop_config.json` och starta om Claude Desktop.
- **`publicera_lista_tidskrifter`** — parametern heter nu `ddk_filter` (tidigare `ddc_filter`).
  Anrop med `ddc_filter` ignoreras tyst; använd `ddk_filter`.
- **`fts_tsv`** är nu en `GENERATED ALWAYS AS`-kolumn. Schemamigrationen (M1) körs automatiskt
  vid uppstart. En full omsynk krävs för att backfilla `publicerad`-fält (se nedan).

### Tillagt

- FTS-kolumn `fts_tsv` som generated column med `'simple'::regconfig` — passar blandspråkig
  korpus (svenska + engelska) utan stemming. Indexerar `titel || abstrakt`; `amnesord` (TEXT[])
  utesluts eftersom `array_to_string()` är STABLE (ej IMMUTABLE) och inte tillåts i generated columns.
  PostgreSQL underhåller kolumnen automatiskt vid upsert.
- GIN-index `artikel_fts_idx` på `fts_tsv`.
- Index `artikel_sprak_idx` på `sprak`.
- `_tolka_post()` extraherar nu `pdf_url`, `tidskrift_namn`, `volym_nummer`, `issn`, `licens`
  och `datestamp` — fälten är nu ifyllda för artiklar hämtade via `publicera_hamta_artikel`.
- `spara_artikel()` sparar alla metadatafält inklusive de ovan nämnda.
- Schema-init sker nu vid serveruppstart med `try/except` — servern stannar kvar i Claude Desktop
  även om databasen är tillfälligt nere.
- `installera_schema` (launchd-varianten) kör `launchctl unload` innan `load` — idempotent
  vid ominstallation.
- `openai>=1.0` tillagt i `requirements.txt` (krävs för query-expansion).

### Åtgärdat

- **Bugg 1** — `publicerad=NULL` för alla artiklar: `len("%Y-%m-%d")` är 8 (inte 10), vilket gav
  `"2017-10-23"[:8] = "2017-10-"` som inte gick att tolka. Datum parsas nu med explicita längder:
  `(("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4))`.
- **Bugg 2** — DDC/DDK-inkonsekvens: parametern `ddc_filter` och miljövariabeln `PUBLICERA_DDC_FILTER`
  i config-snippeten var felaktigt namngivna. Alla förekomster är nu `ddk_filter` / `PUBLICERA_DDK_FILTER`.
- **Bugg 3** — Hårdkodad personlig sökväg i `claude_desktop_config_snippet.json` ersatt med
  `/<absolut/sökväg/till>/`-platshållare.
- **Bugg 4** — Tre olika DDL-scheman (i `01_inventera_tidskrifter.py`, `02_synka_metadata.py` och
  `mcp_server.py`) med motstridiga kolumnnamn. Alla är nu justerade mot kanoniskt schema i
  `mcp_server.py:ensure_schema()`.
- **Bugg 5** — Felaktig Python-standardsökväg i `installera_schema`: `"../../.venv/bin/python3"`
  pekade på `~/.venv/` (existerar ej). Nu `"../.venv/bin/python3"` relativt skriptmappen,
  dvs. `~/MCP-Servers/.venv/bin/python3`.

### Konventioner (icke-bakåtbrytande)

- Interna variabler `_ddc_arg` → `_ddk_arg` och `extra_ddc` → `extra_ddk` i `_sok()`.
- Miljövariabel `PYTHON_SOKVÄG` → `PYTHON_SOKVAG` (tar bort icke-ASCII Ä).
- `_tolka_post()` har fått docstring som beskriver DC-formatet på Publicera.
- Migration-block med tydlig rubrik och datumkommentar i `ensure_schema()`.
- README utökad med sektionerna "Förutsättningar", "Daglig synk" och "Licens".

### Krävd åtgärd efter uppgradering

Kör en full omsynk för att backfilla `publicerad`-fältet för befintliga artiklar:

```
python3 02_synka_metadata.py --force
```

---

