# RDT Connect native localization — catalog v5

**Status: catalog v5 reconciliation candidate.** The English catalog, 25 translated catalogs, inventory, and stable source-key map are maintained as one set. Structural validation does not certify translation quality or platform-specific runtime coverage; those reviews remain separate gates.

## Scope and changes
- Native RDT Connect source only. No edits to `~/dev/rdt-voice` or embedded browser locale files.
- English catalog has 1,305 messages: 1,180 `TRANSLATE`, 125 `KEEP_ENGLISH`, across 26 locale catalogs.
- v10 package carries forward the previously audited repairs the remaining audited pseudo-plural/placeholder issues in stop-flow messages; uses semantic Rust format placeholders; and classifies the backend-candidate dump records as technical diagnostics that stay in English.
- Remaining translated messages with positional `{argN}` placeholders have definitions in the inventory `placeholder_notes` field.
- Catalog version is 5. Existing message IDs remain stable; no key IDs were regenerated during the v5 reconciliation. Existing surviving message keys are retained by `source_to_key_map.json`; do not regenerate keys from English text.
- Earlier v5 repairs to rich help messages, CA confirmation plurals, cache-cleanup wording, implementation-token exclusions, and obsolete language-selector strings remain included.

## Important limitation
The 26 native locale JSON catalogs are present in the current RDT Connect tree. This metadata contract does not prove that every message is wired into every platform-specific UI branch. Windows Notification Area and Linux System Tray wording remain distinct from macOS menu-bar and Apple MLX copy. Embedded RDT Voice browser locale files are outside this catalog.

## Validation
From the `rdt_connect` directory, run `python3 localization/validate_locales.py src/locales --english-only` for the English/inventory contract and `python3 localization/validate_locales.py src/locales` for all 26 catalogs. The full validator checks key parity, locale metadata, placeholder parity, `KEEP_ENGLISH` values, semantic diagnostic placeholders, and recorded artifact hashes. Translation quality and actual per-platform rendering require separate review.

## Translator policy
- `TRANSLATE`: translate the full message naturally, preserving every placeholder exactly.
- `KEEP_ENGLISH`: copy the English string byte-for-byte into each locale. Raw diagnostics are intentionally English.
- No implementation-only keys or incomplete fragments may be reintroduced.
- Preserve safe rich-message slot placeholders literally; do not add HTML or markup.

## Frozen artifact hashes
- English catalog SHA-256: `c47861d3bfc578dd85ee4a73b5f6b5072b784b62f28631430983a8d4c25c44f3`
- Inventory SHA-256: `902bf91e22e1380d8fbf3d46cdc2c5038fff823d7ac0bf8f69f5d105760be87e`
- Source-key map SHA-256: `c533d54b0376046e13faf1ce8b2c860ab43373f3e636c3cf3a62a60038fe7bd9`

## What has and has not been tested
- Passed: catalog/inventory validation; guarded installer dry-run on a disposable project created from the exact baseline; install and rollback on disposable files; rejection of a path-traversal manifest; refusal to rollback over a changed target; ZIP and embedded checksum validation.
- Not run: Rust/Tauri compilation or native macOS runtime. These remain required gates after the guarded patch is dry-run and applied on the user's Mac.


## V10 audit fixes

The four audited raw process-snapshot, manager-process, and port-listener diagnostics now use semantically named placeholders in Rust source and the English catalog while remaining KEEP_ENGLISH. The locale validator verifies exact required placeholder sets and per-placeholder notes for those messages. Installer and rollback reject symlinked ancestor components even when a symlink resolves to another directory within the app root; paths are checked again before mutation.


## V10 validation hardening

The validator compares each inventory row's `placeholders` field against the exact sorted, distinct placeholder names extracted from that row's English catalog value. Any missing, extra, duplicate, or misordered inventory placeholder token fails validation. Installer and rollback both inspect every lexical absolute-path component (including ancestors above the project root) for symlinks before trusting the path; the code does not resolve first and thereby hide symlink ancestors. Regression tests cover incorrect inventory placeholder fields, project-root/testing symlinks, clean rollback, modified installed files, and path traversal.


## V10 safeguards

The validator and installer compare every inventory `placeholders` field with the exact sorted set of named placeholders in the English string. Missing, extra, duplicate, or incorrectly recorded placeholder names fail validation. Installer and rollback reject symlinks in any lexical absolute path component, including components above the project root, before calling path resolution. Regression tests intentionally corrupt an inventory placeholder field and introduce a `project/testing` symlink to confirm both cases fail safely.


## V9 metadata consistency correction

The extraction report sourceMapSha256 and this README’s frozen artifact hashes were reconciled against the actual packaged files. The locale validator and guarded installer now verify the English catalog, inventory, and source-key-map hashes declared in extraction_report.json; a stale or corrupted report hash fails validation before installation.

Key-migration report SHA-256: `f8179896fab1c33992bea7891d2797e52b48372fa67fd4601d524eed8acc0526`.

## Catalog v5 reconciliation — 2026-11-23

The v5 reconciliation retained all existing catalog key IDs and added 315 already-present English/locale catalog entries to the translation inventory and stable source-key map. The resulting set contains 1,305 messages: 1,180 `TRANSLATE` and 125 `KEEP_ENGLISH`.

The three added `KEEP_ENGLISH` entries are the canonical technical values `Apple MLX`, `GPU`, and a format-only statistics template. The other 312 additions remain translatable; existing translated locale values were retained. The existing `updater.offline` inventory key was also restored to the source-key map.

Seven stale inventory/map English values were aligned to the current English catalog without rewriting the English catalog. The three Whisper model labels are byte-for-byte English in every translated catalog. The GGML/CPU baseline label has a localized catalog entry in every translated locale. Mac-specific Apple MLX, Keychain and macOS copy was not globally rewritten.

Some catalog entries still lack a recovered current application-source reference. Their inventory/source-map context is explicitly marked unverified rather than inventing a source location. Review those entries and platform-specific rendering before release.
