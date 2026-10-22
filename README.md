# Gothic 3 Stringtable Translator

Find and fill in **missing translations** in Gothic 3 `stringtable.ini` files with the
[DeepL API](https://www.deepl.com/pro-api) - for a whole modpack at once, safely, with backups.

Many Gothic 3 mods ship with only one or two languages. The other language columns are empty,
contain a `-`, or a placeholder such as `[GER]`. This tool detects those gaps, translates the
text from the best available language and writes the result back into the file.

> **Not affiliated with** Gothic 3, Piranha Bytes, THQ Nordic, DeepL or any mod author.
> Machine translations are a starting point, not a replacement for human proofreading.

---

## Features

- **Two tools, one config**
  - `stringtable_translator.py` - translates and writes the files
  - `check_missing_translations.py` - read-only preparation tool (changes nothing)
- **Any target language** - `DE, EN, FR, IT, ES, CS, HU, PL, RU, ZH`, a list (`DE, EN`) or
  **`ALL`** (wildcard) to fill every missing language, with a warning and explicit approval
- **Smart gap detection** - empty cells, `-`, placeholder tags (`[DE]`, `[GE]`, `[GER]` ... for
  every language) and optional foreign-language copies (`[ENG] Hello`)
- **Smart source selection** - only real entries are used as a source, in a priority order you control
- **Metadata is never touched** - `CurrentLanguage`, `CurrentRevision` & co. are ignored
- **Safe by design** - backups before every change, atomic file writes, original encoding and line
  endings preserved, one bad line never stops a run
- **Quota aware** - estimates characters, requests and duration, compares them with your DeepL
  quota, respects per-run character limits and request rates
- **Cache** - a text is never sent to DeepL twice
- **Clear output** - step-by-step console output with progress bar and ETA, plus detailed logs

## Requirements

- Python 3.8 or newer
- A DeepL API key ([Free plan](https://www.deepl.com/pro-api): 500,000 characters/month)
- `pip install deepl` (only needed by the translator; the check tool works without it)

## Quick start

```bash
git clone https://github.com/maximal-max/gothic3-stringtable-translator.git
cd <repo-name>
pip install -r requirements.txt

# 1. create your config
copy config.example.ini config.ini        # Windows   (Linux/macOS: cp)

# 2. edit config.ini: set mods_folder and target_language

# 3. set your API key (recommended: environment variable, not the config file)
set DEEPL_API_KEY=your-key-here           # Windows cmd   (PowerShell: $env:DEEPL_API_KEY="...")

# 4. look first - nothing is changed
python check_missing_translations.py

# 5. translate
python stringtable_translator.py
```

Command line overrides (optional):

```bash
python stringtable_translator.py "D:\Mods"     # different mods folder
python stringtable_translator.py --lang EN     # different target language
python stringtable_translator.py --lang ALL    # wildcard (asks for approval)
python stringtable_translator.py --config my.ini
```

## How it works

For each entry (`key=EN;;IT;;FR;;DE;;ES;;CS;;HU;;PL;;RU;;ZH`) and each target language:

| Cell content in the target language | Result |
|---|---|
| `Hallo Welt` (real text) | kept |
| *(empty)* | **missing -> translate** |
| `-` | **missing -> translate** |
| `[GER]`, `[GE]`, `[DE]` (placeholder only) | **missing -> translate** |
| `[ENG] Hello world` (foreign-language copy) | **missing -> translate** (can be switched off) |
| `[GER] Hallo` (own-language tag + text) | kept |

The source text is taken from the first language in `source_priority` that has a **real** entry
(not empty, not `-`, not only a tag). A tag in a source cell defines its real language, so
`[DE] Hallo` in the Italian column is treated as German.

Lines that are **ignored completely**:

- entries that are empty or only `-` (`key=`, `key=;;;;`, `key=-`)
- lines with only placeholders/dashes in every language
- comments (`;`, `#`, `//`) and lines without `=`
- metadata: keys in `ignore_keys` (`CurrentLanguage`, `CurrentRevision`) and - as a safety net -
  lines whose value contains no `;`

## Placeholder tags

A tag is a language marker in square brackets at the **start** of a cell, e.g. `[GER]` or
`[GER] Hallo`. Matching is case-insensitive and spaces inside the brackets are allowed (`[ ger ]`).
The built-in list covers ISO 639 codes, Windows locale codes, common mod abbreviations and English
and native language names:

| Language | Recognized tags |
|---|---|
| English | `EN` `ENG` `ENGL` `ENU` `EN-US` `EN-GB` `ENGLISH` `UK` `USA` |
| Italian | `IT` `ITA` `ITAL` `ITALIAN` `ITALIANO` |
| French | `FR` `FRE` `FRA` `FRENCH` `FRANCAIS` `FRANÇAIS` `FRANC` |
| German | `DE` `GE` `GER` `DEU` `GERM` `GERMAN` `DEUTSCH` `DEUT` |
| Spanish | `ES` `ESP` `SPA` `SP` `SPAN` `ESN` `SPANISH` `ESPANOL` `ESPAÑOL` `CASTELLANO` |
| Czech | `CS` `CZ` `CZE` `CES` `CSY` `CZECH` `CESKY` `ČESKY` `CESTINA` `ČEŠTINA` |
| Hungarian | `HU` `HUN` `HUNG` `HUNGARIAN` `MAG` `MAGY` `MAGYAR` |
| Polish | `PL` `POL` `PLK` `POLISH` `POLSKI` `POLSKA` |
| Russian | `RU` `RUS` `RUSS` `RUSSIAN` `RUSSKIJ` `RUSSKIY` |
| Chinese | `ZH` `CN` `CHI` `CHN` `ZHO` `CHS` `CHT` `CHINESE` `ZH-CN` `ZH-TW` `ZH-HANS` `ZH-HANT` |

Missing one? Add it **without touching the code** in the `[tags]` section of `config.ini`:

```ini
[tags]
hu = MAGYARUL, MG
de = ALLEMAND
```

A tag can only belong to one language; conflicts are reported at start-up. The built-in table
lives at the top of `stringtable_translator.py` (`LANGS`).

## The check tool

Run it **before** translating. It never modifies a mod file; it only writes logs.

It tells you how many files are affected, how many conflicts exist per target language, how many
characters and API requests are needed, how long it will take, and whether your quota is enough.
With an API key it reads your real quota from DeepL; otherwise it uses `monthly_char_limit`
from the config.

It also writes one log per affected file to `logs/check/` listing every conflict with line number,
key, reason, available languages and the language it would translate from.

## Wildcard mode (`target_language = ALL`)

Fills the missing translation of **every** language (minus `wildcard_exclude`) from the best
available source. Because this can touch many files and cost many characters, the translator
shows a warning with the numbers and only continues if you type `YES`. `--yes` does not skip this
step, and it refuses to run without an interactive console.

Short lines (fewer columns than a language needs) are not extended unless
`wildcard_pad_missing_columns = true`.

## Configuration

Everything is configured in `config.ini` (copy it from `config.example.ini`). Both tools use the
same file. Important options:

| Section | Option | Meaning |
|---|---|---|
| `[paths]` | `mods_folder` | Folder searched recursively (or one `.ini` file) |
| | `backup_folder`, `log_folder`, `cache_file` | Where backups, logs and the cache live |
| `[deepl]` | `api_key` | Your key. Prefer the environment variable `DEEPL_API_KEY` (it wins) |
| `[translation]` | `target_language` | `DE`, `EN`, ..., `DE, EN` or `ALL` |
| | `wildcard_exclude` | Languages to skip in wildcard mode, e.g. `ZH, RU` |
| | `source_priority` | Which language is used as the source first |
| | `foreign_tag_is_missing` | Treat `[ENG] text` in another column as missing |
| | `ignore_keys` | Metadata keys that are never touched (wildcards like `Current*` work) |
| | `skip_entries_without_separator` | Ignore lines without `;` |
| | `target_files` | File names to process |
| `[tags]` | `<language> = TAG1, TAG2` | Extra placeholder tags per language (optional) |
| | `confirm_before_start` | Ask `[Y/n]` before translating |
| `[limits]` | `max_chars_per_run` | Character cap for one run |
| | `monthly_char_limit` | Your plan's quota (used when live usage is unavailable) |
| | `max_requests_per_run`, `request_delay_seconds`, `max_requests_per_minute` | Rate limits |
| | `max_attempts`, `retry_wait_seconds`, `max_consecutive_failures` | Error handling |
| `[backup]` | `mode` | `once` (first original only) or `per_run` (one folder per run) |

## Safety

- **Backups** - every file is copied to `backup_folder` before it is changed.
  Restore a file by copying it back.
- **Atomic writes** - files are written to a temp file and then swapped in; no half-written files.
- **Encoding preserved** - UTF-16, UTF-8 (with/without BOM) and cp1252 files keep their encoding and
  line endings. A text that cannot be stored in a legacy encoding (e.g. Polish in a cp1252 file)
  is skipped and logged; other languages of the same line are still saved.
- **Safe to interrupt** - `Ctrl+C` stops cleanly; finished work is kept and the cache is saved.
- **Resumable** - run it again; already translated entries are no longer missing.
- **Keys stay secret** - the API key is removed from error messages and logs.

## Logs

```
logs/
  translation/   changes_<timestamp>.log      every translation that was written
                 unresolved_<timestamp>.log   entries without usable source + errors
  check/         <mod>__<file>.log            conflicts of one file (line, key, languages)
                 _summary.txt                 overview of the check
  crash_log.txt                               unexpected errors
```

## Troubleshooting

| Problem | Solution |
|---|---|
| `CONFIGURATION FILE NOT FOUND` | Copy `config.example.ini` and name the copy exactly `config.ini` (on Windows make sure it is not `config.ini.txt`). The tool prints step-by-step instructions |
| `CONFIGURATION NOT FINISHED` | `mods_folder` still contains the example path - edit it |
| A placeholder tag is not recognized | Add it under `[tags]` in `config.ini` |
| `no DeepL API key found` | Set `DEEPL_API_KEY` or `[deepl] api_key` |
| `Package 'deepl' is not installed` | `pip install deepl` |
| `quota exhausted` | Wait for the monthly reset or upgrade your plan; the next run continues |
| A line was changed that should not be | Add its key to `ignore_keys` |
| Entries are never translated | `skip_entries_without_separator = false` if they have only one column |
| Garbled special characters | Please open an issue with an example file (first lines) |

## Project layout

```
stringtable_translator.py        main program (detection, estimation, translation)
check_missing_translations.py    read-only check tool (imports from the translator)
config.example.ini               template for your config.ini
requirements.txt
```

Both scripts must stay in the same folder.

## Contributing

Issues and pull requests are welcome. When reporting a problem, please include the first lines of an
affected stringtable file (without private data) and the relevant part of `logs/crash_log.txt`.

## License

MIT - see [LICENSE](LICENSE).