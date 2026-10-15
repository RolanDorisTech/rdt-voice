# Translation job: RDT Connect native interface — catalog v10

**Do not begin until the v10 independent audit approves the catalog.** This package is for the native RDT Connect desktop interface only. Do not edit application code or touch the separate RDT Voice browser project.

English catalog SHA-256: `ee4833815b195b3e83450bac29ae0cd0ada9457219f2cc878d729466fc36ef42`. Catalog version: `4`.

## Classification policy
The adjacent `localization/translation_inventory.csv` is authoritative.
- `TRANSLATE` (759 entries): translate naturally, especially ordinary-user guidance, buttons, tooltips, status, errors, and accessibility text.
- `KEEP_ENGLISH` (122 entries): copy the English value exactly into each locale. These are technical diagnostics, raw logs, identifiers, or selected developer-facing strings.
- There are no `EXCLUDE` or unresolved `REWRITE_ENGLISH` entries in the catalog. Never re-add internal tokens or retired fragments.

Words and identifiers such as port, HTTP, HTTPS, PID, URL, IP addresses, paths, command names, model IDs, Keychain, Tailscale, Whisper and product names may stay English where appropriate. Translate the explanations that help ordinary users. Developer Options and diagnostic output may appropriately retain technical English.

## Output schema
Create JSON files named exactly for: `zh-CN`, `zh-TW`, `fr`, `es`, `ko`, `ja`, `de`, `th`, `vi`, `tl`, `id`, `hi`, `ms`, `pt`, `ru`, `it`, `lo`, `ar`, `tr`, `pl`, `nl`, `uk`, `sv`, `cs`, `no`. Each file must match `en.json`: `_meta.locale` exact code, `_meta.languageName` native endonym, `_meta.direction` `rtl` only for Arabic (`ar`) and `ltr` otherwise, `_meta.catalogVersion` `4`, `_meta.source` `RDT Connect native app only`; `messages` must contain exactly the same keys.

## Rules
1. Do not add, delete, or rename keys.
2. Translate only `TRANSLATE`; copy `KEEP_ENGLISH` values exactly.
3. Preserve every named placeholder exactly, including case and spelling. Do not drop or duplicate placeholders. The inventory `placeholder_notes` explains values like ports, process IDs, paths, counts, error details, and technical status fields.
4. Rich-message placeholders refer to existing DOM slots. Preserve them literally; do not add HTML or markup.
5. Translate whole sentences, not word fragments. Do not reintroduce split clauses, suffix plurals, or `{count} item(s)` forms.
6. Do not modify source code, Rust strings, the key map, or browser-project resources. Return translations only.

## Locale batches
- Batch 1: zh-CN, zh-TW, fr, es, ko
- Batch 2: ja, de, th, vi, tl
- Batch 3: id, hi, ms, pt, ru
- Batch 4: it, lo, ar, tr, pl
- Batch 5: nl, uk, sv, cs, no

For each batch ZIP, include exactly the locale JSONs plus `BATCH_MANIFEST.json` with `locales` listing those codes and `english_catalog_sha256` equal to `ee4833815b195b3e83450bac29ae0cd0ada9457219f2cc878d729466fc36ef42`. Validate using `validate_locales.py --allow-partial` against this exact English catalog. Do not claim completion if keys, placeholders, metadata, empty values, or `KEEP_ENGLISH` equality fail.


V8 placeholder contract note: raw diagnostic messages classified KEEP_ENGLISH are still required to use semantically named placeholders in the source/catalog map. Preserve those English values exactly in translated locale files; do not translate raw diagnostics. For translated entries, preserve exact placeholder names and use the inventory's placeholder_notes to understand each dynamic value. Never make up positional-argument meanings.

Key-migration report SHA-256: `7ae795613f59b2ec4b4f7a70ee3dcba252ad0f7b01c4917b3c20c3cf72c956fe`. Use catalogVersion 4 in every locale.
