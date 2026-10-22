# -*- coding: utf-8 -*-
"""
Stringtable Translator (DeepL)

Searches the configured folder for stringtable files, finds missing translations
for the configured target language(s) and fills them in with DeepL.

A translation is missing when the target column is
  * empty, or only "-", or
  * only a placeholder tag such as [DE], [GE], [GER] (or the equivalent of any language), or
  * (optional) a foreign-language copy such as "[ENG] Hello".
The text is translated from the best available language, i.e. a column that
contains a REAL entry (not empty, not "-", not only a tag).
Entries that are completely empty, only "-", or metadata lines such as
CurrentLanguage / CurrentRevision are ignored.

Usage:
    python stringtable_translator.py
    python stringtable_translator.py --config other.ini
    python stringtable_translator.py --lang EN          (override target language)
    python stringtable_translator.py --lang ALL         (wildcard, asks for approval)
    python stringtable_translator.py "D:\\Mods"          (override mods folder)

All settings live in config.ini.
To only look (nothing is changed), use check_missing_translations.py - it imports
the detection/estimation code from this file, so both tools always agree.

File layout:
    1. Settings (config.ini)          5. Analysis (read-only)
    2. Safety net + progress bar      6. Cache + estimation
    3. Cell classification            7. DeepL client + file processing (writes!)
    4. File reading/writing           8. Main program
"""

import argparse
import configparser
import fnmatch
import io
import json
import math
import os
import re
import shutil
import stat
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.ini"

# ============================================================================
# LANGUAGE TABLE
#   index = position of the language inside an entry (values split by ';')
#   tags  = every spelling that is accepted as a placeholder tag, e.g. [GER], [GE], [DE].
#           Matching rules: square brackets, at the START of the cell, case-insensitive,
#           spaces inside the brackets are allowed ("[ GER ]").
#           Covered: ISO 639-1/2/3 codes, Windows locale codes, common mod abbreviations,
#           English names and native names (e.g. Hungarian "MAG" / "MAGYAR").
#           A tag may belong to ONE language only (checked at start-up).
#           More tags can also be added without touching the code: see [tags] in config.ini.
# ============================================================================
LANGS = {
    "EN": {"index": 0, "name": "English", "tags": (
        "EN", "ENG", "ENGL", "ENU", "EN-US", "EN-GB", "ENGLISH", "UK", "USA")},
    "IT": {"index": 2, "name": "Italian", "tags": (
        "IT", "ITA", "ITAL", "ITALIAN", "ITALIANO")},
    "FR": {"index": 4, "name": "French", "tags": (
        "FR", "FRE", "FRA", "FRENCH", "FRANCAIS", "FRANÇAIS", "FRANC")},
    "DE": {"index": 6, "name": "German", "tags": (
        "DE", "GE", "GER", "DEU", "GERM", "GERMAN", "DEUTSCH", "DEUT")},
    "ES": {"index": 8, "name": "Spanish", "tags": (
        "ES", "ESP", "SPA", "SP", "SPAN", "ESN", "SPANISH", "ESPANOL", "ESPAÑOL", "CASTELLANO")},
    "CS": {"index": 10, "name": "Czech", "tags": (
        "CS", "CZ", "CZE", "CES", "CSY", "CZECH", "CESKY", "ČESKY", "CESTINA", "ČEŠTINA")},
    "HU": {"index": 12, "name": "Hungarian", "tags": (
        "HU", "HUN", "HUNG", "HUNGARIAN", "MAG", "MAGY", "MAGYAR")},
    "PL": {"index": 14, "name": "Polish", "tags": (
        "PL", "POL", "PLK", "POLISH", "POLSKI", "POLSKA")},
    "RU": {"index": 16, "name": "Russian", "tags": (
        "RU", "RUS", "RUSS", "RUSSIAN", "RUSSKIJ", "RUSSKIY")},
    "ZH": {"index": 18, "name": "Chinese", "tags": (
        "ZH", "CN", "CHI", "CHN", "ZHO", "CHS", "CHT", "CHINESE", "ZH-CN", "ZH-TW", "ZH-HANS", "ZH-HANT")},
}

# Languages whose letters cannot be stored in a legacy (non-Unicode) file encoding.
NEEDS_UNICODE = {"CS", "HU", "PL", "RU", "ZH"}

# Built by build_tag_tables(): alias (upper-case) -> language code, and the regex that matches
# a leading tag such as "[GER]" or "[ ger ]" (longest aliases first).
TAG_ALIASES = {}
TAG_RE = None


def build_tag_tables(extra=None):
    """(Re)build TAG_ALIASES and TAG_RE from LANGS plus optional extra tags from config.ini.
    `extra` maps a language code to a list of additional tags.
    Raises ValueError if a tag would belong to two different languages."""
    global TAG_ALIASES, TAG_RE
    aliases = {}

    def add(code, alias):
        alias = alias.strip().upper()
        if not alias:
            return
        owner = aliases.get(alias)
        if owner and owner != code:
            raise ValueError(f"Tag [{alias}] is assigned to both {owner} and {code}")
        aliases[alias] = code

    for code, info in LANGS.items():
        for alias in info["tags"]:
            add(code, alias)
    for code, tags in (extra or {}).items():
        for alias in tags:
            add(code, alias)

    TAG_ALIASES = aliases
    TAG_RE = re.compile(
        r"^\s*\[\s*(" + "|".join(sorted(map(re.escape, aliases), key=len, reverse=True)) + r")\s*\]\s*",
        re.IGNORECASE,
    )


build_tag_tables()      # built-in tags; config.ini may add more when the settings are loaded

# Format tokens (%s, {0}, <b> ...) that a translation must not damage.
FORMAT_TOKEN_RE = re.compile(r"%[sdif]|\{[^}]*\}|<[^>]+>")


# ============================================================================
# SETTINGS (loaded from config.ini)
# ============================================================================
class ConfigError(Exception):
    """Raised when config.ini contains invalid values."""


class ConfigMissing(ConfigError):
    """Raised when the configuration file does not exist."""

    def __init__(self, path):
        super().__init__(f"Configuration file not found: {path}")
        self.path = Path(path)


# Value of mods_folder in config.example.ini - if it is still there, the user forgot to edit it.
EXAMPLE_MODS_FOLDER_MARKER = "path\\to\\your\\mods"


def report_config_error(exc):
    """Print a helpful, step-by-step message for configuration problems (both tools use this)."""
    if not isinstance(exc, ConfigMissing):
        print("=" * 72)
        print("CONFIGURATION ERROR")
        print("=" * 72)
        print(exc)
        print("\nPlease fix config.ini and start the tool again.")
        return

    folder = exc.path.parent
    example = folder / "config.example.ini"
    print("=" * 72)
    print("CONFIGURATION FILE NOT FOUND")
    print("=" * 72)
    print(f"Looked for:  {exc.path}")
    print()
    print("Both tools need a configuration file. By default it must be called exactly")
    print("    config.ini")
    print("and lie in the same folder as the scripts.")
    print()
    print("How to fix it:")
    if example.exists():
        print(f"  1. Copy  config.example.ini  and name the copy  config.ini")
    else:
        print("  1. Download config.example.ini from the project page, put it next to the scripts,")
        print("     copy it and name the copy  config.ini")
    print("     (Windows: make sure it is not called 'config.ini.txt' - enable")
    print("      View > File name extensions in the Explorer.)")
    print("  2. Open config.ini in a text editor and set at least:")
    print("        mods_folder     = full path of your Mods folder")
    print("        target_language = DE, EN, FR, ... or ALL")
    print("  3. Provide your DeepL API key (translator only), either")
    print("        - as environment variable DEEPL_API_KEY (recommended), or")
    print("        - in config.ini under [deepl] api_key")
    print("  4. Start the tool again.")
    print()
    print("Using a config file with another name or location:")
    print("    python stringtable_translator.py --config \"C:\\path\\to\\my_config.ini\"")

    # Typical mistakes: show what is actually lying in the folder.
    try:
        similar = sorted(p.name for p in folder.iterdir()
                         if p.is_file() and p.name.lower().startswith("config") and p.name != "config.example.ini")
    except OSError:
        similar = []
    if similar:
        print()
        print("Found in that folder (wrong name?):  " + ", ".join(similar))
    print("=" * 72)


def check_mods_folder(settings):
    """Verify mods_folder. Prints a helpful message and returns False if it cannot be used."""
    folder = settings.mods_folder
    if EXAMPLE_MODS_FOLDER_MARKER in str(folder).lower():
        print("=" * 72)
        print("CONFIGURATION NOT FINISHED")
        print("=" * 72)
        print("mods_folder in your config file still contains the example path:")
        print(f"    {folder}")
        print("Open config.ini and set mods_folder to the real path of your Mods folder.")
        print("=" * 72)
        return False
    if not folder.exists():
        print("=" * 72)
        print("MODS FOLDER NOT FOUND")
        print("=" * 72)
        print(f"    {folder}")
        print("Check mods_folder in config.ini (typos? drive letter? folder moved?).")
        print("=" * 72)
        return False
    return True


@dataclass
class Settings:
    # paths
    mods_folder: Path
    backup_folder: Path
    log_folder: Path
    cache_file: Path
    # deepl
    api_key: str
    # translation
    target_languages: list        # list of language codes that are checked / filled
    wildcard: bool                # True when target_language = ALL
    wildcard_pad_columns: bool
    source_priority: list
    english_variant: str
    foreign_tag_is_missing: bool
    ignore_keys: list             # lower-case key patterns that are never touched (metadata)
    skip_single_column: bool      # ignore lines whose value has no ';' at all
    target_files: set
    confirm_before_start: bool
    # limits
    max_chars_per_run: int
    monthly_char_limit: int
    max_requests_per_run: int
    request_delay: float
    max_requests_per_minute: int
    max_attempts: int
    retry_wait: float
    max_consecutive_failures: int
    cache_save_every: int
    # backup
    backup_mode: str              # "once" or "per_run"

    @property
    def translation_log_dir(self):
        """Logs of the translator (one change log + one unresolved log per run)."""
        return self.log_folder / "translation"

    @property
    def check_log_dir(self):
        """Logs of the check tool (one log per affected file + summary)."""
        return self.log_folder / "check"


def _split_list(raw):
    """'DE, EN;FR' -> ['DE', 'EN', 'FR'] (upper-case, empty items removed)."""
    return [item.strip().upper() for item in re.split(r"[,;\s]+", raw) if item.strip()]


def _split_keys(raw):
    """'CurrentLanguage, Current*' -> ['CurrentLanguage', 'Current*'] (case is kept, wildcards allowed)."""
    return [item.strip() for item in re.split(r"[,;]+", raw) if item.strip()]


def _clean_path(value, base):
    """Strip quotes, expand ~ and resolve relative paths against the config folder."""
    path = Path(os.path.expanduser(value.strip().strip('"').strip("'")))
    return path if path.is_absolute() else (base / path)


def load_settings(config_path=None, lang_override=None, path_override=None):
    """Read config.ini and return a validated Settings object.
    Raises ConfigError with a readable message when something is wrong."""
    config_path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
    if not config_path.exists():
        raise ConfigMissing(config_path)

    parser = configparser.ConfigParser(interpolation=None)   # no % interpolation (Windows paths)
    try:
        parser.read(config_path, encoding="utf-8-sig")
    except configparser.Error as exc:
        raise ConfigError(f"Cannot parse {config_path.name}: {exc}")

    base = config_path.resolve().parent
    problems = []

    def get(section, key, cast=str, default=None):
        """Read one value with type conversion; collect problems instead of crashing."""
        try:
            if cast is bool:
                return parser.getboolean(section, key, fallback=default)
            if cast is int:
                return parser.getint(section, key, fallback=default)
            if cast is float:
                return parser.getfloat(section, key, fallback=default)
            return parser.get(section, key, fallback=default)
        except ValueError:
            problems.append(f"[{section}] {key}: invalid value '{parser.get(section, key)}'")
            return default

    # ---- paths ----
    mods = path_override or get("paths", "mods_folder", default="")
    if not str(mods).strip():
        problems.append("[paths] mods_folder is empty")

    # ---- target languages (single, list or ALL wildcard) ----
    raw_target = lang_override or get("translation", "target_language", default="DE")
    requested = _split_list(raw_target)
    excluded = _split_list(get("translation", "wildcard_exclude", default=""))
    wildcard = "ALL" in requested
    if wildcard:
        targets = [code for code in LANGS if code not in excluded]
    else:
        targets = requested
    unknown = [c for c in targets + excluded if c not in LANGS]
    if unknown:
        problems.append(f"Unknown language code(s): {', '.join(unknown)} (allowed: {', '.join(LANGS)}, ALL)")
    if not targets:
        problems.append("[translation] target_language is empty")

    priority = _split_list(get("translation", "source_priority", default="EN, DE, FR, IT, ES, PL, CS, HU, RU, ZH"))
    bad = [c for c in priority if c not in LANGS]
    if bad:
        problems.append(f"[translation] source_priority has unknown language(s): {', '.join(bad)}")

    backup_mode = get("backup", "mode", default="once").strip().lower()
    if backup_mode not in ("once", "per_run"):
        problems.append("[backup] mode must be 'once' or 'per_run'")

    english_variant = get("translation", "english_variant", default="EN-US").strip().upper()
    if english_variant not in ("EN-US", "EN-GB"):
        problems.append("[translation] english_variant must be EN-US or EN-GB")

    # ---- optional extra placeholder tags: [tags] section, e.g.  hu = MAGYARUL, MG ----
    extra_tags = {}
    if parser.has_section("tags"):
        for code_raw, value in parser.items("tags"):
            code = code_raw.strip().upper()
            if code not in LANGS:
                problems.append(f"[tags] '{code_raw}' is not a known language (allowed: {', '.join(LANGS)})")
                continue
            extra_tags[code] = _split_list(value)
    try:
        build_tag_tables(extra_tags)
    except ValueError as exc:
        problems.append(f"[tags] {exc}")

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))

    # file names are compared case-insensitively -> store them lower-case
    target_files = {name.strip().lower() for name in re.split(r"[,;]+", get(
        "translation", "target_files", default="stringtable.ini, stringtableexpress.ini")) if name.strip()}

    return Settings(
        mods_folder=_clean_path(str(mods), base),
        backup_folder=_clean_path(get("paths", "backup_folder", default="_backup"), base),
        log_folder=_clean_path(get("paths", "log_folder", default="logs"), base),
        cache_file=_clean_path(get("paths", "cache_file", default="translation_cache_v2.json"), base),
        # the environment variable always wins over the config file
        api_key=os.environ.get("DEEPL_API_KEY", "").strip() or get("deepl", "api_key", default="").strip(),
        target_languages=targets,
        wildcard=wildcard,
        wildcard_pad_columns=get("translation", "wildcard_pad_missing_columns", bool, False),
        source_priority=priority,
        english_variant=english_variant,
        foreign_tag_is_missing=get("translation", "foreign_tag_is_missing", bool, True),
        ignore_keys=[k.lower() for k in _split_keys(get("translation", "ignore_keys",
                     default="CurrentLanguage, CurrentRevision"))],
        skip_single_column=get("translation", "skip_entries_without_separator", bool, True),
        target_files=target_files,
        confirm_before_start=get("translation", "confirm_before_start", bool, True),
        max_chars_per_run=get("limits", "max_chars_per_run", int, 450_000),
        monthly_char_limit=get("limits", "monthly_char_limit", int, 500_000),
        max_requests_per_run=get("limits", "max_requests_per_run", int, 0),
        request_delay=get("limits", "request_delay_seconds", float, 0.2),
        max_requests_per_minute=get("limits", "max_requests_per_minute", int, 0),
        max_attempts=get("limits", "max_attempts", int, 3),
        retry_wait=get("limits", "retry_wait_seconds", float, 5.0),
        max_consecutive_failures=get("limits", "max_consecutive_failures", int, 5),
        cache_save_every=get("limits", "cache_save_every", int, 20),
        backup_mode=backup_mode,
    )


def target_label(settings):
    """Human readable description of the target languages for headers/logs."""
    if settings.wildcard:
        return f"ALL (wildcard): {', '.join(settings.target_languages)}"
    return ", ".join(f"{LANGS[c]['name']} ({c})" for c in settings.target_languages)


def deepl_target_code(code, settings):
    """DeepL requires a variant for English targets (EN-US / EN-GB)."""
    return settings.english_variant if code == "EN" else code


def request_interval(settings):
    """Minimum number of seconds between two API requests (delay + per-minute cap)."""
    interval = settings.request_delay
    if settings.max_requests_per_minute > 0:
        interval = max(interval, 60.0 / settings.max_requests_per_minute)
    return interval


# ============================================================================
# SAFETY NET (console, crash log, secret redaction)
# ============================================================================
SECRETS = []   # the API key is registered here so it never shows up in output or logs
CRASH_LOG = SCRIPT_DIR / "logs" / "crash_log.txt"


def setup_console():
    """Avoid crashes caused by special characters in the Windows console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def redact(text):
    """Remove API keys from any text (requests libraries sometimes echo them)."""
    text = str(text)
    text = re.sub(r"(auth_key|api_key|authorization)(=|:\s*)[^&\s'\"]+", r"\1\2***", text, flags=re.I)
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "***")
    return text


def log_error(context, exc=None):
    """Append an error with traceback to the crash log. Never raises itself."""
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {context}\n")
            handle.write(f"Python {sys.version.split()[0]} | {sys.executable}\n")
            if exc is not None:
                handle.write(redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))))
            handle.write("-" * 70 + "\n")
    except Exception:
        pass


def safe_input(prompt):
    """input() that never raises (no console, closed stdin, Ctrl+C ...)."""
    try:
        return input(prompt)
    except (Exception, KeyboardInterrupt):
        return ""


def _crash_hook(exc_type, exc, tb):
    """Last line of defence: keep the window open and show what went wrong."""
    print("\n" + "=" * 70)
    print("UNEXPECTED ERROR (the window stays open so you can read this):")
    print("=" * 70)
    print(redact("".join(traceback.format_exception(exc_type, exc, tb))))
    log_error("Uncaught error", exc)
    print(f"\nDetails were also written to: {CRASH_LOG}")
    safe_input("\nPress ENTER to exit...")


def install_safety_net(log_folder=None):
    """Call once at program start."""
    global CRASH_LOG
    if log_folder:
        CRASH_LOG = Path(log_folder) / "crash_log.txt"
    setup_console()
    sys.excepthook = _crash_hook


# ============================================================================
# PROGRESS BAR
# ============================================================================
def fmt_time(seconds):
    """Seconds -> 'mm:ss' or 'h:mm:ss'."""
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class Progress:
    """Single-line progress bar with percentage and ETA.
    Falls back to plain lines when the output is not a console."""
    BAR_WIDTH = 28

    def __init__(self, total):
        self.total = total
        self.start = time.time()
        self.is_tty = sys.stdout.isatty()
        self.last_len = 0
        self.last_plain = 0

    def update(self, done, info=""):
        if self.total <= 0:
            return
        ratio = min(1.0, done / self.total)
        filled = int(self.BAR_WIDTH * ratio)
        bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
        elapsed = time.time() - self.start
        eta = fmt_time(elapsed / done * (self.total - done)) if done > 0 and elapsed > 2 else "--:--"
        line = f"[{bar}] {ratio * 100:5.1f}%  {done}/{self.total}  ETA {eta}  {info}"
        if self.is_tty:
            width = shutil.get_terminal_size((110, 20)).columns - 1
            line = line[:width]
            sys.stdout.write("\r" + line.ljust(self.last_len))
            sys.stdout.flush()
            self.last_len = len(line)
        elif done - self.last_plain >= 25 or done >= self.total:
            print(line)
            self.last_plain = done

    def log(self, message):
        """Print a normal message without destroying the bar."""
        if self.is_tty and self.last_len:
            sys.stdout.write("\r" + " " * self.last_len + "\r")
            self.last_len = 0
        print(message)

    def close(self):
        if self.is_tty and self.last_len:
            print()
            self.last_len = 0


# ============================================================================
# CELL CLASSIFICATION (the heart of the placeholder logic)
# ============================================================================
@dataclass(frozen=True)
class Cell:
    """One language column of one entry.
    kind:
      EMPTY  - nothing useful: empty, only '-', or only placeholder tag(s) like [GER]
      TAGGED - a tag followed by real text, e.g. '[ENG] Hello' (a foreign-language copy)
      PLAIN  - real text without a tag
    """
    kind: str
    tag_lang: str     # language code of the leading tag, or None
    text: str         # text without leading tags ('' for EMPTY)
    note: str         # short human-readable description used in logs


EMPTY_CELL = Cell("EMPTY", None, "", "empty")


def split_tags(text):
    """Strip leading tags. '[ENG] [x] Hello' -> ('EN', '[ENG]', '[x] Hello').
    Returns (language of first tag or None, first tag as written or None, remaining text)."""
    text = text.strip()
    lang = tag_raw = None
    while True:
        match = TAG_RE.match(text)
        if not match:
            break
        if lang is None:
            lang = TAG_ALIASES[match.group(1).upper()]
            tag_raw = f"[{match.group(1)}]"
        text = text[match.end():].strip()
    return lang, tag_raw, text


def classify_cell(raw):
    """Classify the raw value of one language column."""
    tag_lang, tag_raw, text = split_tags(raw)
    if text in ("", "-"):
        if tag_raw:
            note = f"placeholder tag {tag_raw}"
        elif text == "-":
            note = "dash only"
        else:
            note = "empty"
        return Cell("EMPTY", tag_lang, "", note)
    if tag_lang:
        return Cell("TAGGED", tag_lang, text, f"copy tagged {tag_raw}")
    return Cell("PLAIN", None, text, "")


def conflict_reason(cell, language, foreign_tag_is_missing):
    """Return why this cell counts as 'missing' for `language`, or None if it is fine."""
    if cell.kind == "EMPTY":
        return cell.note
    if cell.kind == "TAGGED" and cell.tag_lang != language and foreign_tag_is_missing:
        return f"foreign-language {cell.note}"
    return None


def pick_source(cells, target, priority):
    """Choose the translation source for `target`.
    Only cells with a REAL entry qualify (never empty / '-' / tag-only).
    A tag on the cell overrides the column's language (e.g. '[DE] Hallo' in the IT column is German).
    Returns (text, language) or (None, None)."""
    for code in priority:
        if code == target:
            continue
        cell = cells.get(code)
        if cell is None or cell.kind == "EMPTY":
            continue
        language = cell.tag_lang or code
        if language == target:
            continue                      # would translate a language into itself
        return cell.text, language
    return None, None


def has_letters(text):
    """False for texts like '12345' or '---' that need no translation."""
    return any(ch.isalpha() for ch in text)


def sanitize_translation(text):
    """Remove characters that would break the INI line format."""
    text = text.replace(";", ",")                       # ';' separates the language columns
    return text.replace("\r", " ").replace("\n", " ").strip()


def format_tokens_match(source, translated):
    """True if %s / {0} / <tag> tokens survived the translation unchanged."""
    return sorted(FORMAT_TOKEN_RE.findall(source)) == sorted(FORMAT_TOKEN_RE.findall(translated))


# ============================================================================
# FILE READING / WRITING
# ============================================================================
def read_file(path):
    """Read a file line by line (line endings preserved). Returns (lines, encoding).
    The encoding is detected from the BOM / content."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ["utf-16"]
    elif raw.startswith(b"\xef\xbb\xbf"):
        candidates = ["utf-8-sig"]
    elif b"\x00" in raw[:200]:
        candidates = ["utf-16-le", "utf-16-be"]
    else:
        candidates = ["utf-8", "cp1252"]
    for encoding in candidates:
        try:
            return io.StringIO(raw.decode(encoding), newline="").readlines(), encoding
        except UnicodeError:
            continue
    raise ValueError(f"Could not detect the encoding of {path}")


def split_line_ending(line):
    """'abc\\r\\n' -> ('abc', '\\r\\n')."""
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[:-len(ending)], ending
    return line, ""


def base_folder(settings):
    """Folder that relative display paths and backups are based on."""
    folder = settings.mods_folder
    return folder if folder.is_dir() else folder.parent


def relative_name(path, settings):
    try:
        return str(Path(path).relative_to(base_folder(settings)))
    except ValueError:
        return Path(path).name


def mod_name(path, settings):
    """Top-level mod folder below the mods folder."""
    try:
        parts = Path(path).relative_to(base_folder(settings)).parts
        return parts[0] if len(parts) > 1 else "[root folder]"
    except ValueError:
        return Path(path).parent.name


def write_atomic(path, data):
    """Write to a temp file first, then replace the original -> never a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        try:
            os.chmod(path, stat.S_IWRITE)             # remove read-only flag (Windows)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def backup_file(path, settings, run_stamp):
    """Copy the original file into the backup folder BEFORE it is modified.
    mode 'once'    : only the very first original is kept (never overwritten)
    mode 'per_run' : every run gets its own timestamped folder"""
    root = settings.backup_folder / run_stamp if settings.backup_mode == "per_run" else settings.backup_folder
    destination = root / relative_name(path, settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(path, destination)


# ============================================================================
# SCANNING AND ANALYSIS (read-only)
# ============================================================================
@dataclass
class Conflict:
    """One missing translation: one target language of one entry."""
    target: str
    reason: str
    available: list           # language codes that have a real entry on this line
    source_text: str = None   # None -> nothing to translate from
    source_lang: str = None


@dataclass
class LineIssue:
    """One line of a file with at least one conflict."""
    idx: int                  # 0-based line index
    key: str
    parts: list               # raw values of all columns
    newline: str
    raw: str                  # the original line without line ending
    conflicts: list = field(default_factory=list)

    @property
    def line_no(self):
        return self.idx + 1


@dataclass
class FilePlan:
    path: Path
    rel: str
    encoding: str
    issues: list = field(default_factory=list)

    @property
    def conflicts(self):
        return [c for issue in self.issues for c in issue.conflicts]


def find_files(settings):
    """Find all stringtable files below mods_folder (or the single file itself)."""
    root = settings.mods_folder
    if root.is_file():
        return [root]
    wanted = settings.target_files
    skip = {settings.backup_folder.resolve(), settings.log_folder.resolve()}
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # never descend into our own backup/log folders
        dirnames[:] = [d for d in dirnames if (Path(dirpath) / d).resolve() not in skip]
        for name in filenames:
            if name.lower() in wanted:
                found.append(Path(dirpath) / name)
    return sorted(found)


def is_metadata(key, rest, settings):
    """True for lines that are not translatable entries, e.g.
        CurrentLanguage=English
        CurrentRevision=0
    Detected by (1) the key list ignore_keys (wildcards like 'Current*' allowed) and
    (2) optionally by the missing ';' - real entries always have several language columns."""
    name = key.strip().lower()
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in settings.ignore_keys):
        return True
    return settings.skip_single_column and ";" not in rest


def analyze_lines(lines, settings):
    """Find all conflicts in the given lines.
    Returns (list of LineIssue, number of ignored empty entries, number of ignored metadata lines)."""
    issues, empty, meta = [], 0, 0

    for idx, line in enumerate(lines):
        content, newline = split_line_ending(line)
        stripped = content.strip()

        # comments, blank lines and lines that are not 'key=value'
        if not stripped or stripped.startswith((";", "#", "//")) or "=" not in content:
            continue
        key, rest = content.split("=", 1)
        if not key.strip():
            continue

        # Entry without any content ('key=', 'key=;;;;' or 'key=-'): ignored completely.
        if rest.replace(";", "").strip() in ("", "-"):
            empty += 1
            continue

        # Metadata such as CurrentLanguage / CurrentRevision: never touched.
        if is_metadata(key, rest, settings):
            meta += 1
            continue

        parts = rest.split(";")
        cells = {code: classify_cell(parts[info["index"]])
                 for code, info in LANGS.items() if info["index"] < len(parts)}
        available = [code for code, cell in cells.items() if cell.kind != "EMPTY"]

        # Only placeholders / dashes everywhere -> nothing real to work with: ignore.
        if not available:
            empty += 1
            continue

        conflicts = []
        for target in settings.target_languages:
            column = LANGS[target]["index"]
            if column < len(parts):
                cell = cells[target]
            elif settings.wildcard and not settings.wildcard_pad_columns:
                continue                  # wildcard: do not extend short lines
            else:
                cell = EMPTY_CELL         # explicit target: the missing column counts as empty

            reason = conflict_reason(cell, target, settings.foreign_tag_is_missing)
            if not reason:
                continue
            text, language = pick_source(cells, target, settings.source_priority)
            conflicts.append(Conflict(target, reason, list(available), text, language))

        if conflicts:
            issues.append(LineIssue(idx, key, parts, newline, content, conflicts))

    return issues, empty, meta


def analyze_files(files, settings, show_progress=True):
    """Analyze all files (read-only).
    Returns (plans of files with conflicts, error messages, Counter of ignored lines)."""
    plans, errors, ignored = [], [], Counter()
    progress = Progress(len(files)) if show_progress else None
    for number, path in enumerate(files, start=1):
        rel = relative_name(path, settings)
        if progress:
            progress.update(number - 1, f"Analyzing: {rel}")
        try:
            lines, encoding = read_file(path)
            issues, empty, meta = analyze_lines(lines, settings)
            ignored["empty"] += empty
            ignored["meta"] += meta
            if issues:
                plans.append(FilePlan(path, rel, encoding, issues))
        except Exception as exc:
            errors.append(f"{rel}: {exc}")
            if progress:
                progress.log(f"   Warning: could not read {rel} ({exc})")
            log_error(f"Analysis of {path}", exc)
    if progress:
        progress.update(len(files), "Analysis finished")
        progress.close()
    return plans, errors, ignored


# ============================================================================
# TRANSLATION CACHE
# ============================================================================
class TranslationCache:
    """Persistent cache: texts that were translated once are never sent again.
    Keys look like 'DE::Hello world' so every target language has its own entries."""

    def __init__(self, path, save_every=20):
        self.path = Path(path)
        self.save_every = save_every
        self.unsaved = 0
        self.data = self._load()

    @staticmethod
    def _read(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            print(f"Warning: cache file {path.name} is damaged and will be ignored.")
            log_error(f"Cache {path} unreadable", exc)
            return {}

    def _load(self):
        if self.path.exists():
            return self._read(self.path)
        # One-time import of the very old German-only cache ('text' -> 'translation').
        legacy = self.path.with_name("translation_cache.json")
        if legacy != self.path and legacy.exists():
            return {f"DE::{text}": value for text, value in self._read(legacy).items()}
        return {}

    def get(self, target, text):
        return self.data.get(f"{target}::{text}")

    def put(self, target, text, translated):
        self.data[f"{target}::{text}"] = translated
        self.unsaved += 1
        if self.unsaved >= self.save_every:
            self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self.unsaved = 0
        except Exception as exc:
            print(f"Warning: cache could not be saved: {exc}")
            log_error("Cache save", exc)

    def __len__(self):
        return len(self.data)


# ============================================================================
# ESTIMATION (used by the translator summary and by the check tool)
# ============================================================================
@dataclass
class Estimate:
    files_total: int = 0
    files_affected: int = 0
    conflicts: int = 0                      # all missing translations
    translatable: int = 0                   # ... that have a source text
    no_source: int = 0                      # ... without any usable source
    cached: int = 0                         # translatable and already in the cache
    copy_only: int = 0                      # texts without letters (copied, no API call)
    requests: int = 0                       # unique API requests still needed
    chars: int = 0                          # characters those requests will cost
    by_target: Counter = field(default_factory=Counter)
    by_target_chars: Counter = field(default_factory=Counter)
    ignored_empty: int = 0                  # entries without any content
    ignored_meta: int = 0                   # metadata lines (CurrentLanguage, ...)


def estimate(plans, files_total, cache, settings, ignored=None):
    """Count conflicts and estimate the API cost (unique texts, cache hits excluded)."""
    est = Estimate(files_total=files_total, files_affected=len(plans))
    if ignored:
        est.ignored_empty, est.ignored_meta = ignored["empty"], ignored["meta"]
    seen = set()
    for plan in plans:
        for conflict in plan.conflicts:
            est.conflicts += 1
            est.by_target[conflict.target] += 1
            if not conflict.source_text:
                est.no_source += 1
                continue
            est.translatable += 1
            text = conflict.source_text
            if not has_letters(text):
                est.copy_only += 1
            elif cache is not None and cache.get(conflict.target, text):
                est.cached += 1
            elif (conflict.target, text) not in seen:
                seen.add((conflict.target, text))
                est.requests += 1
                est.chars += len(text)
                est.by_target_chars[conflict.target] += len(text)
    return est


def fetch_quota(api_key):
    """Ask DeepL for (used, limit) characters. Returns None if that is not possible."""
    try:
        import deepl
        usage = deepl.Translator(api_key).get_usage()
        if usage.character.valid:
            return usage.character.count, usage.character.limit
    except Exception as exc:
        log_error("DeepL usage query", exc)
    return None


@dataclass
class Assessment:
    remaining: int            # characters left in the quota
    budget: int               # characters this run may use (min of quota and per-run cap)
    live: bool                # True when quota numbers came from DeepL
    used: int                 # characters already used (0 if unknown)
    limit: int
    runs: int                 # number of runs needed to finish everything
    seconds: float            # rough duration of all requests
    verdict: str


def assess(est, settings, quota):
    """Compare the estimated cost with the quota and the configured limits."""
    live = quota is not None
    used, limit = quota if live else (0, settings.monthly_char_limit)
    remaining = max(0, limit - used)
    budget = min(settings.max_chars_per_run, remaining)

    runs = 1
    if est.chars > 0:
        runs = max(1, math.ceil(est.chars / budget)) if budget > 0 else 0
    if settings.max_requests_per_run > 0 and est.requests > 0:
        runs = max(runs, math.ceil(est.requests / settings.max_requests_per_run))

    seconds = est.requests * (request_interval(settings) + 0.4)   # ~0.4 s assumed API latency

    if est.chars == 0:
        verdict = "OK - nothing has to be sent to the API."
    elif est.chars > remaining:
        verdict = (f"NOT ENOUGH QUOTA - short by {est.chars - remaining:,} characters. "
                   "A run translates what fits; the rest has to wait for the quota reset or a bigger plan.")
    elif runs > 1:
        verdict = (f"ENOUGH QUOTA, but the per-run limits split the work into {runs} runs "
                   "(max_chars_per_run / max_requests_per_run).")
    else:
        verdict = "OK - the quota is sufficient for a single run."
    return Assessment(remaining, budget, live, used, limit, runs, seconds, verdict)


def print_overview(plans, est, assessment, settings):
    """Console report shared by the translator (before it starts) and the check tool."""
    # --- per mod ---
    by_mod = {}
    for plan in plans:
        info = by_mod.setdefault(mod_name(plan.path, settings), [0, 0])
        info[0] += 1
        info[1] += len(plan.conflicts)
    if by_mod:
        print("\nAffected mods:")
        for name, (file_count, conflict_count) in sorted(by_mod.items(), key=lambda kv: kv[1][1], reverse=True):
            print(f"  - {name:<45} {conflict_count:>6} conflicts in {file_count} file(s)")

    # --- totals ---
    print("\n" + "-" * 72)
    print(f"Files checked:               {est.files_total}")
    print(f"Files with conflicts:        {est.files_affected}")
    print(f"Conflicts (missing entries): {est.conflicts}")
    for code in settings.target_languages:
        if est.by_target[code]:
            print(f"   {LANGS[code]['name']:<12}({code}):        {est.by_target[code]:>6}"
                  f"   (~{est.by_target_chars[code]:,} chars)")
    print(f"   translatable:             {est.translatable}  (already cached: {est.cached}, no letters/copied: {est.copy_only})")
    print(f"   without usable source:    {est.no_source}")
    print(f"Ignored (no content):        {est.ignored_empty}")
    print(f"Ignored (metadata lines):    {est.ignored_meta}   (ignore_keys / entries without ';')")

    # --- API load ---
    print("\nAPI load estimate:")
    print(f"   Characters to translate:  {est.chars:,}  (unique texts, cache hits excluded)")
    print(f"   API requests:             {est.requests:,}")
    print(f"   Request rate:             1 request / {request_interval(settings):.2f} s "
          f"(max {60 / request_interval(settings):.0f} per minute)")
    print(f"   Estimated duration:       ~{fmt_time(assessment.seconds)}")
    if assessment.live:
        print(f"   Monthly quota (DeepL):    {assessment.used:,} / {assessment.limit:,} used, "
              f"{assessment.remaining:,} remaining")
    else:
        print(f"   Monthly quota (config):   {assessment.limit:,} (live usage unknown, assuming nothing used)")
    print(f"   Budget for one run:       {assessment.budget:,}  "
          f"(min of per-run cap {settings.max_chars_per_run:,} and remaining quota)")
    if assessment.remaining > 0 and est.chars > 0:
        print(f"   Share of remaining quota: {est.chars / assessment.remaining * 100:.1f} %")
    print(f"   Verdict:                  {assessment.verdict}")

    # --- encoding hint ---
    risky = [p for p in plans if not p.encoding.startswith(("utf", "UTF"))]
    needs = [c for c in settings.target_languages if c in NEEDS_UNICODE]
    if risky and needs:
        print(f"\nNote: {len(risky)} file(s) use a legacy encoding ({risky[0].encoding}) that cannot store "
              f"{', '.join(needs)} text. Affected lines will be skipped and reported.")
    print("-" * 72)


# ============================================================================
# DeepL CLIENT (rate limiting, retries, budgets)
# ============================================================================
class TranslationAbort(Exception):
    """The run cannot sensibly continue (quota, key, repeated failures, limits)."""


class DeepLClient:
    # Fallback detection by error text (used when no specific exception class matches)
    QUOTA_MARKERS = ("456", "quota")
    AUTH_MARKERS = ("403", "unauthorized", "authorization", "authentication", "api key")
    TRANSIENT_MARKERS = ("429", "too many requests", "502", "503", "504", "server error",
                         "timeout", "timed out", "connection", "max retries", "temporarily")

    def __init__(self, settings, cache, quota):
        try:
            import deepl
        except Exception as exc:
            raise TranslationAbort(
                f"Package 'deepl' is not installed ({exc}). Install it with:\n"
                f"   {sys.executable} -m pip install deepl")
        self.s = settings
        self.cache = cache
        self.translator = deepl.Translator(settings.api_key)

        # Specific exception classes (empty tuple = "catch nothing" if a class is missing)
        self.E_QUOTA = getattr(deepl, "QuotaExceededException", ())
        self.E_AUTH = getattr(deepl, "AuthorizationException", ())
        self.E_RETRY = tuple(getattr(deepl, n) for n in ("TooManyRequestsException", "ConnectionException")
                             if hasattr(deepl, n))

        # Budgets: the per-run cap AND the remaining DeepL quota both apply.
        self.budget = settings.max_chars_per_run
        if quota:
            self.budget = min(self.budget, max(0, quota[1] - quota[0]))
        self.chars_used = 0
        self.requests = 0
        self.fail_streak = 0
        self._next_allowed = 0.0                 # rate limiter: earliest start of the next request
        self.interval = request_interval(settings)

    def _throttle(self):
        """Wait so that requests never start faster than the configured rate."""
        wait = self._next_allowed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_allowed = time.monotonic() + self.interval

    def _register_failure(self, reason):
        """Count a failed text; abort cleanly when too many fail in a row."""
        self.fail_streak += 1
        if self.fail_streak >= self.s.max_consecutive_failures:
            raise TranslationAbort(f"{self.fail_streak} translations failed in a row "
                                   f"(last error: {reason}). Please try again later.")

    def translate(self, text, source_lang, target_lang):
        """Return the translation, or None if it failed (will be retried in the next run).
        Raises TranslationAbort for quota / key / limit problems."""
        cached = self.cache.get(target_lang, text)
        if cached:
            return cached

        if self.chars_used + len(text) > self.budget:
            raise TranslationAbort(f"Character budget for this run reached ({self.budget:,}). "
                                   "The next run continues where this one stopped.")
        if self.s.max_requests_per_run and self.requests >= self.s.max_requests_per_run:
            raise TranslationAbort(f"Request limit for this run reached ({self.s.max_requests_per_run}). "
                                   "The next run continues where this one stopped.")

        last_error = "unknown"
        for attempt in range(self.s.max_attempts):
            try:
                self._throttle()
                self.requests += 1
                result = self.translator.translate_text(
                    text, source_lang=source_lang,
                    target_lang=deepl_target_code(target_lang, self.s))
                self.chars_used += len(text)
                self.fail_streak = 0
                out = result.text
                if isinstance(out, str) and out.strip():
                    self.cache.put(target_lang, text, out)
                    return out
                return None

            except self.E_QUOTA:
                raise TranslationAbort("DeepL quota exhausted.")
            except self.E_AUTH:
                raise TranslationAbort("DeepL API key is invalid or not authorized.")
            except self.E_RETRY as exc:
                last_error = redact(exc)[:200]
                time.sleep(self.s.retry_wait * (attempt + 1))
            except Exception as exc:
                last_error = redact(exc)[:200]
                message = str(exc).lower()
                if any(m in message for m in self.QUOTA_MARKERS):
                    raise TranslationAbort("DeepL quota exhausted.")
                if any(m in message for m in self.AUTH_MARKERS):
                    raise TranslationAbort("DeepL API key is invalid or not authorized.")
                if any(m in message for m in self.TRANSIENT_MARKERS):
                    time.sleep(self.s.retry_wait * (attempt + 1))
                    continue
                self._register_failure(last_error)      # unknown error: do not retry
                return None

        self._register_failure(last_error)              # all retries used up
        return None


# ============================================================================
# FILE PROCESSING
# ============================================================================
@dataclass
class Run:
    """Everything that is collected during one run (for logs and the final report)."""
    progress: Progress
    stamp: str                                        # timestamp used for per_run backups
    stats: Counter = field(default_factory=Counter)
    per_target: Counter = field(default_factory=Counter)
    changes: list = field(default_factory=list)       # log text blocks of applied translations
    unresolved: list = field(default_factory=list)    # log text blocks of entries without source
    errors: list = field(default_factory=list)        # file level errors
    line_errors: list = field(default_factory=list)
    done: int = 0                                     # finished translation jobs (progress)


def unresolved_block(plan, issue, conflict):
    """Log text for an entry that has no usable source text."""
    return (f"File: {plan.rel}\nLine: {issue.line_no}\nKey: {issue.key}\n"
            f"Target: {conflict.target}\nReason: {conflict.reason}\n"
            f"Original line:\n{issue.raw}\n{'-' * 80}\n")


def process_file(plan, file_no, file_count, settings, client, run):
    """Translate all conflicts of one file and write it back.
    Returns an abort message if the whole run has to stop, otherwise None."""
    lines, encoding = read_file(plan.path)              # read fresh from disk
    issues, _, _ = analyze_lines(lines, settings)
    new_lines = list(lines)
    counters = Counter()
    abort = None

    for issue in issues:
        applied = []                                         # (conflict, translated text, warning)

        for conflict in issue.conflicts:
            run.progress.update(run.done, f"File {file_no}/{file_count}: {plan.rel}")

            # No real source text on this line: report it, nothing to translate.
            if not conflict.source_text:
                counters["no_source"] += 1
                run.unresolved.append(unresolved_block(plan, issue, conflict))
                continue

            try:
                if has_letters(conflict.source_text):
                    translated = client.translate(conflict.source_text, conflict.source_lang, conflict.target)
                else:
                    translated = conflict.source_text        # numbers/symbols only: copy as is
                    counters["copied"] += 1
                run.done += 1

                translated = sanitize_translation(translated) if translated else None
                if not translated:
                    counters["pending"] += 1                 # left untouched, retried next run
                    continue

                warning = "" if format_tokens_match(conflict.source_text, translated) \
                    else "WARNING: format tokens (%s, {0}, <..>) differ - please check!\n"
                applied.append((conflict, translated, warning))

            except TranslationAbort as stop:
                abort = str(stop)
                break
            except KeyboardInterrupt:
                abort = "Stopped by user."
                break
            except Exception as exc:                         # one bad entry never stops the run
                counters["line_errors"] += 1
                run.line_errors.append(f"{plan.rel} line {issue.line_no}: {exc}")
                log_error(f"Error in {plan.rel}, line {issue.line_no}", exc)

        # Apply the translations of this line (also when the run is being aborted).
        # Each translation is checked against the file's encoding on its own, so one text that
        # cannot be stored (e.g. Polish in a cp1252 file) does not block the others.
        parts_now = list(issue.parts)
        kept = []
        for item in applied:
            conflict, translated, _ = item
            column = LANGS[conflict.target]["index"]
            candidate = list(parts_now)
            while len(candidate) <= column:                  # extend short lines if necessary
                candidate.append("")
            candidate[column] = translated
            try:
                (issue.key + "=" + ";".join(candidate) + issue.newline).encode(encoding)
            except UnicodeEncodeError:
                counters["encoding_skipped"] += 1
                run.line_errors.append(f"{plan.rel} line {issue.line_no}: {conflict.target} text cannot be "
                                       f"stored in encoding {encoding} - left unchanged")
                continue
            parts_now = candidate
            kept.append(item)

        if kept:
            new_lines[issue.idx] = issue.key + "=" + ";".join(parts_now) + issue.newline
            for conflict, translated, warning in kept:
                counters["translated"] += 1
                run.per_target[conflict.target] += 1
                if warning:
                    run.stats["token_warnings"] += 1
                run.changes.append(
                    f"File: {plan.rel}\nLine: {issue.line_no}\nKey: {issue.key}\n"
                    f"Target: {LANGS[conflict.target]['name']} ({conflict.target})\n"
                    f"Reason: {conflict.reason}\n"
                    f"Source ({LANGS[conflict.source_lang]['name']}): {conflict.source_text}\n"
                    f"Result: {translated}\n{warning}{'-' * 60}\n")
        if abort:
            break

    # Write the file only if something changed: backup first, then atomic replace.
    if counters["translated"] > 0:
        data = "".join(new_lines).encode(encoding)
        backup_file(plan.path, settings, run.stamp)
        write_atomic(plan.path, data)
        run.stats["files_changed"] += 1

    for name in ("translated", "pending", "no_source", "copied", "encoding_skipped", "line_errors"):
        run.stats[name] += counters[name]
    run.stats["files_done"] += 1

    summary = [f"{counters['translated']} translated"]
    for name, label in (("pending", "pending"), ("no_source", "without source"),
                        ("copied", "copied unchanged"), ("encoding_skipped", "skipped (encoding)")):
        if counters[name]:
            summary.append(f"{counters[name]} {label}")
    run.progress.log(f"  [{file_no}/{file_count}] {plan.rel}: " + ", ".join(summary))
    return abort


# ============================================================================
# LOGS
# ============================================================================
def write_logs(run, client, settings, abort_message):
    """Write the logs of this run into <log_folder>/translation/ (two files, named by timestamp):
         changes_<stamp>.log     every translation that was written
         unresolved_<stamp>.log  entries without usable source + errors"""
    folder = settings.translation_log_dir
    folder.mkdir(parents=True, exist_ok=True)
    change_log = folder / f"changes_{run.stamp}.log"
    unresolved_log = folder / f"unresolved_{run.stamp}.log"
    try:
        with open(change_log, "w", encoding="utf-8") as handle:
            handle.write(f"RUN {datetime.now():%Y-%m-%d %H:%M:%S} | targets: {target_label(settings)}\n")
            handle.write("=" * 70 + "\n\n")
            handle.write("".join(run.changes) if run.changes else "No translations were made.\n")
            handle.write(f"\nTranslated in this run: {run.stats['translated']}\n")
            handle.write(f"Characters used in this run: {client.chars_used if client else 0}\n")
            if abort_message:
                handle.write(f"Run ended early: {abort_message}\n")
    except Exception as exc:
        print(f"Warning: change log could not be written: {exc}")
        log_error("Change log", exc)

    try:
        content = ["ENTRIES WITHOUT A USABLE SOURCE TEXT\n", "=" * 80 + "\n\n",
                   "".join(run.unresolved) or "None.\n",
                   f"\nTotal: {run.stats['no_source']}\n"]
        if run.errors:
            content += ["\nFILES WITH ERRORS:\n", "\n".join(run.errors) + "\n"]
        if run.line_errors:
            content += ["\nLINES WITH ERRORS:\n", "\n".join(run.line_errors) + "\n"]
        unresolved_log.write_text("".join(content), encoding="utf-8")
    except Exception as exc:
        print(f"Warning: unresolved log could not be written: {exc}")
        log_error("Unresolved log", exc)
    return change_log, unresolved_log


# ============================================================================
# USER CONFIRMATION
# ============================================================================
def confirm_wildcard(est, assessment, settings):
    """Mandatory warning + explicit approval for wildcard mode. Returns True if approved."""
    print("\n" + "!" * 72)
    print("WARNING: WILDCARD MODE (target_language = ALL)")
    print("!" * 72)
    print("Every missing translation in EVERY language will be filled via DeepL:")
    print(f"   {', '.join(settings.target_languages)}")
    print(f"   Entries to translate : {est.translatable:,} in {est.files_affected} file(s)")
    print(f"   Characters (approx.) : {est.chars:,}  of {assessment.remaining:,} remaining")
    print(f"   {assessment.verdict}")
    print(f"Original files are backed up first ({settings.backup_folder}), but machine translations")
    print("will be written into many files and many languages.")
    print("!" * 72)
    if not sys.stdin.isatty():
        print("Wildcard mode needs an interactive console for the approval. Aborting.")
        return False
    return safe_input("Type YES (capital letters) to continue, anything else cancels: ").strip() == "YES"


def confirm_normal(settings, assume_yes):
    """Simple [Y/n] question before translating."""
    if assume_yes or not settings.confirm_before_start or not sys.stdin.isatty():
        return True
    return safe_input("\nStart translating now? [Y/n] ").strip().lower() not in ("n", "no")


# ============================================================================
# MAIN
# ============================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Stringtable Translator (DeepL)")
    parser.add_argument("path", nargs="?", help="override mods_folder from the config")
    parser.add_argument("--config", help="path to a config file (default: config.ini next to the script)")
    parser.add_argument("--lang", help="override target_language (e.g. DE, EN, DE,EN or ALL)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="skip the normal [Y/n] question (has no effect in wildcard mode)")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    try:
        settings = load_settings(args.config, args.lang, args.path.strip('"') if args.path else None)
    except ConfigError as exc:
        report_config_error(exc)
        return
    install_safety_net(settings.log_folder)

    if not check_mods_folder(settings):
        return
    if not settings.api_key:
        print("ERROR: no DeepL API key found.")
        print("Set it in config.ini ([deepl] api_key) or via the environment variable DEEPL_API_KEY.")
        return
    SECRETS.append(settings.api_key)
    settings.log_folder.mkdir(parents=True, exist_ok=True)
    settings.backup_folder.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("STRINGTABLE TRANSLATOR (DeepL)")
    print("=" * 72)
    print(f"Folder:   {settings.mods_folder}")
    print(f"Target:   {target_label(settings)}")
    print(f"Sources:  {' > '.join(settings.source_priority)}  (target language is skipped)")
    print(f"Backups:  {settings.backup_folder}  (mode: {settings.backup_mode})")
    print("=" * 72)

    # ---- Step 1: connect ----
    print("\n[1/4] Connecting to DeepL ...")
    cache = TranslationCache(settings.cache_file, settings.cache_save_every)
    quota = fetch_quota(settings.api_key)
    try:
        client = DeepLClient(settings, cache, quota)
    except TranslationAbort as exc:
        print(f"ERROR: {exc}")
        return
    except Exception as exc:
        print(f"ERROR: could not start the DeepL client: {redact(exc)}")
        log_error("DeepLClient()", exc)
        return
    if quota:
        print(f"      Quota: {quota[0]:,} of {quota[1]:,} characters used")
    else:
        print("      Quota: could not be queried (continuing with the configured limits)")
    print(f"      Cache: {len(cache):,} stored translations")

    # ---- Step 2: find files ----
    print("\n[2/4] Searching stringtable files ...")
    files = find_files(settings)
    print(f"      {len(files)} file(s) found")
    if not files:
        return

    # ---- Step 3: analyze ----
    print("\n[3/4] Analyzing files ...")
    plans, scan_errors, ignored = analyze_files(files, settings)
    est = estimate(plans, len(files), cache, settings, ignored)
    assessment = assess(est, settings, quota)
    print_overview(plans, est, assessment, settings)

    run = Run(progress=Progress(est.translatable), stamp=datetime.now().strftime("%Y%m%d_%H%M%S"),
              errors=list(scan_errors))

    if est.conflicts == 0:
        print("\nNothing to do - no missing translations found.")
        return
    if est.translatable == 0:
        print("\nNothing can be translated - only entries without a usable source text (see log).")
        for plan in plans:
            for issue in plan.issues:
                for conflict in issue.conflicts:
                    run.stats["no_source"] += 1
                    run.unresolved.append(unresolved_block(plan, issue, conflict))
        write_logs(run, client, settings, None)
        return

    # ---- approval ----
    if settings.wildcard:
        if args.yes:
            print("\nNote: --yes is ignored in wildcard mode.")
        if not confirm_wildcard(est, assessment, settings):
            print("Cancelled. Nothing was changed.")
            return
    elif not confirm_normal(settings, args.yes):
        print("Cancelled. Nothing was changed.")
        return

    # ---- Step 4: translate ----
    print(f"\n[4/4] Translating {est.translatable} entries in {len(plans)} file(s) ...  "
          "(Ctrl+C stops safely)\n")
    abort_message = None
    try:
        for number, plan in enumerate(plans, start=1):
            try:
                abort_message = process_file(plan, number, len(plans), settings, client, run)
            except Exception as exc:
                run.errors.append(f"{plan.rel}: {exc}")
                run.progress.log(f"  ERROR in {plan.rel}: {redact(exc)}")
                run.progress.log("     File was not modified. Finished translations stay in the cache.")
                log_error(f"File {plan.path}", exc)
                continue
            if abort_message:
                break
    except KeyboardInterrupt:
        abort_message = "Stopped by user."
    finally:
        run.progress.update(run.done, "Finished" if not abort_message else "Stopped")
        run.progress.close()
        cache.save()
        change_log, unresolved_log = write_logs(run, client, settings, abort_message)

    # ---- result ----
    stats = run.stats
    print("\n" + "=" * 72)
    print("RESULT")
    print("=" * 72)
    print(f"Files processed:         {stats['files_done']} of {len(plans)}")
    print(f"Files modified:          {stats['files_changed']}")
    print(f"Translated:              {stats['translated']}")
    for code in settings.target_languages:
        if run.per_target[code]:
            print(f"   {LANGS[code]['name']:<12}({code}):    {run.per_target[code]}")
    print(f"Still pending (re-run):  {stats['pending']}")
    print(f"Without usable source:   {stats['no_source']}")
    print(f"Skipped (file encoding): {stats['encoding_skipped']}")
    print(f"Format token warnings:   {stats['token_warnings']}")
    print(f"Failed files / lines:    {len(run.errors)} / {stats['line_errors']}")
    print(f"Characters used:         {client.chars_used:,}   API requests: {client.requests:,}")
    if abort_message:
        print(f"\nRun ended early: {abort_message}")
    print(f"\nLogs folder:    {settings.translation_log_dir}")
    print(f"   {change_log.name}\n   {unresolved_log.name}")
    print(f"Backups:        {settings.backup_folder}")
    print(f"Cache:          {settings.cache_file}")
    if run.errors or stats["line_errors"]:
        print(f"Error details:  {CRASH_LOG}")
    print("=" * 72)


if __name__ == "__main__":
    install_safety_net()
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except SystemExit:
        pass
    except BaseException as exc:
        print("\n" + "=" * 72)
        print(f"UNEXPECTED ERROR: {redact(exc)}")
        print("=" * 72)
        print(redact(traceback.format_exc()))
        log_error("Unexpected error in main()", exc)
        print(f"\nDetails were also written to: {CRASH_LOG}")
    finally:
        safe_input("\nPress ENTER to exit...")