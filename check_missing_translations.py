# -*- coding: utf-8 -*-
"""
Check Missing Translations  -  READ-ONLY preparation tool

Run this BEFORE the translator. It never modifies any mod file. It tells you:
  * how many files are affected and how many conflicts (missing translations)
    exist per target language,
  * how many characters / API requests the translation would need, how long it
    takes, and whether your DeepL quota is sufficient,
  * and it writes one log file per affected file that lists every conflict
    with its line number and the languages that are available on that line.

It uses the SAME config.ini and the same detection logic as the translator
(it imports it from stringtable_translator.py, so both files must stay in the same
folder), therefore the numbers match what the translator will do.
The only things written are the log files in <log_folder>/check/.

Usage:
    python check_missing_translations.py
    python check_missing_translations.py --lang EN
    python check_missing_translations.py --config other.ini
"""

import argparse
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

import stringtable_translator as core     # shared engine: settings, analysis, estimation
from stringtable_translator import LANGS


def is_inside(path, folder):
    """True if `path` lies inside `folder` (used as a safety guard for the log folder)."""
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False


def log_file_name(rel, used):
    """'ModA/sub/stringtable.ini' -> 'ModA__sub__stringtable.ini.log' (unique within this run)."""
    name = re.sub(r"[\\/:]+", "__", rel)
    name = re.sub(r'[<>"|?*]', "_", name) + ".log"
    candidate, counter = name, 2
    while candidate in used:
        candidate = f"{name[:-4]}_{counter}.log"
        counter += 1
    used.add(candidate)
    return candidate


def write_file_log(plan, settings, folder, name):
    """Write the conflict log of ONE file: every conflict with line number and available languages."""
    conflicts = plan.conflicts
    lines = [
        f"CONFLICT LOG: {plan.rel}",
        f"Generated:  {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Full path:  {plan.path}",
        f"Encoding:   {plan.encoding}",
        f"Target(s):  {core.target_label(settings)}",
        f"Conflicts:  {len(conflicts)} (on {len(plan.issues)} line(s))",
        "=" * 78,
        "",
    ]
    for issue in plan.issues:
        for conflict in issue.conflicts:
            available = ", ".join(f"{LANGS[c]['name']} ({c})" for c in conflict.available) or "none"
            if conflict.source_text:
                source = f"{LANGS[conflict.source_lang]['name']} ({conflict.source_lang})"
            else:
                source = "NONE - cannot be translated automatically"
            lines += [
                f"Line {issue.line_no}  |  key: {issue.key}",
                f"   Missing language:     {LANGS[conflict.target]['name']} ({conflict.target})",
                f"   Reason:               {conflict.reason}",
                f"   Available languages:  {available}",
                f"   Would translate from: {source}",
                f"   Original line:        {issue.raw}",
                "",
            ]
    (folder / name).write_text("\n".join(lines), encoding="utf-8")


def write_summary(plans, est, assessment, settings, folder, errors):
    """Write one overview file next to the per-file logs."""
    lines = [
        "CHECK SUMMARY",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Folder:    {settings.mods_folder}",
        f"Target(s): {core.target_label(settings)}",
        "=" * 78,
        f"Files checked:        {est.files_total}",
        f"Files with conflicts: {est.files_affected}",
        f"Conflicts total:      {est.conflicts}  (translatable: {est.translatable}, no source: {est.no_source})",
        f"Characters needed:    {est.chars:,}",
        f"API requests:         {est.requests:,}",
        f"Quota remaining:      {assessment.remaining:,} ({'live from DeepL' if assessment.live else 'from config'})",
        f"Verdict:              {assessment.verdict}",
        "",
        "Conflicts per file:",
    ]
    for plan in sorted(plans, key=lambda p: len(p.conflicts), reverse=True):
        lines.append(f"  {len(plan.conflicts):>6}  {plan.rel}")
    if errors:
        lines += ["", "Files that could not be read:", *[f"  {e}" for e in errors]]
    (folder / "_summary.txt").write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Check missing translations (read-only)")
    parser.add_argument("path", nargs="?", help="override mods_folder from the config")
    parser.add_argument("--config", help="path to a config file (default: config.ini next to the script)")
    parser.add_argument("--lang", help="override target_language (e.g. DE, EN, DE,EN or ALL)")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    try:
        settings = core.load_settings(args.config, args.lang, args.path.strip('"') if args.path else None)
    except core.ConfigError as exc:
        core.report_config_error(exc)
        return
    core.install_safety_net(settings.log_folder)

    if not core.check_mods_folder(settings):
        return

    # Safety guard: this tool must never write anything inside the mods folder.
    check_folder = settings.check_log_dir
    if is_inside(check_folder, settings.mods_folder):
        print("ABORTED: the log folder lies inside the mods folder.")
        print("For safety this tool writes nothing there. Change log_folder in config.ini.")
        return

    print("=" * 72)
    print("CHECK MISSING TRANSLATIONS  (read-only - no mod file is changed)")
    print("=" * 72)
    print(f"Folder:   {settings.mods_folder}")
    print(f"Target:   {core.target_label(settings)}")
    print(f"Sources:  {' > '.join(settings.source_priority)}  (target language is skipped)")
    print("=" * 72)

    # ---- Step 1: find files ----
    print("\n[1/3] Searching stringtable files ...")
    files = core.find_files(settings)
    print(f"      {len(files)} file(s) found")
    if not files:
        return

    # ---- Step 2: analyze ----
    print("\n[2/3] Analyzing files ...")
    plans, errors, ignored = core.analyze_files(files, settings)

    # ---- Step 3: estimate API load ----
    print("\n[3/3] Estimating API load ...")
    cache = core.TranslationCache(settings.cache_file, settings.cache_save_every)
    quota = None
    if settings.api_key:
        core.SECRETS.append(settings.api_key)
        quota = core.fetch_quota(settings.api_key)        # read-only query of the usage numbers
        print("      Quota: " + (f"{quota[0]:,} of {quota[1]:,} characters used (live from DeepL)"
                                 if quota else "could not be queried, using monthly_char_limit from config"))
    else:
        print("      No API key set - using monthly_char_limit from config (live usage unknown)")
    print(f"      Cache: {len(cache):,} stored translations (their texts cost nothing)")
    est = core.estimate(plans, len(files), cache, settings, ignored)
    assessment = core.assess(est, settings, quota)
    core.print_overview(plans, est, assessment, settings)

    # ---- per-file logs ----
    check_folder.mkdir(parents=True, exist_ok=True)
    for old in check_folder.glob("*.log"):                # remove logs of earlier checks (stale data)
        try:
            old.unlink()
        except OSError:
            pass
    used_names = set()
    for plan in plans:
        write_file_log(plan, settings, check_folder, log_file_name(plan.rel, used_names))
    write_summary(plans, est, assessment, settings, check_folder, errors)

    print(f"\nLogs: {len(plans)} file log(s) + _summary.txt written to:")
    print(f"   {check_folder}")
    if errors:
        print(f"Warning: {len(errors)} file(s) could not be read (see _summary.txt).")
    if est.conflicts:
        print("\nNext step: run stringtable_translator.py to fill these in.")
    print("=" * 72)


if __name__ == "__main__":
    core.install_safety_net()
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except SystemExit:
        pass
    except BaseException as exc:
        print(f"\nError: {core.redact(exc)}")
        traceback.print_exc()
        core.log_error("Error in check tool", exc)
    finally:
        core.safe_input("\nPress ENTER to exit...")