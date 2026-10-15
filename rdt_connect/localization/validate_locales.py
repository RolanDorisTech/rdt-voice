#!/usr/bin/env python3
"""Validate native RDT Connect locale catalogs against the frozen English catalog and inventory policy."""
from pathlib import Path
import argparse, csv, json, re, hashlib
CODES = ["en", "zh-CN", "zh-TW", "fr", "es", "ko", "ja", "de", "th", "vi", "tl", "id", "hi", "ms", "pt", "ru", "it", "lo", "ar", "tr", "pl", "nl", "uk", "sv", "cs", "no"]
RTL={"ar"}; TOKEN=re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
def load(p):
    try: d=json.loads(p.read_text(encoding='utf-8'))
    except Exception as e: raise ValueError(f"Invalid JSON {p}: {e}")
    if not isinstance(d,dict) or not isinstance(d.get('messages'),dict): raise ValueError(f"Invalid catalog structure: {p.name}")
    return d
def main():
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('root',nargs='?',default='.'); ap.add_argument('--allow-partial',action='store_true'); ap.add_argument('--english-only',action='store_true',help='validate English catalog and inventory without translation files'); args=ap.parse_args(); root=Path(args.root).expanduser().resolve()
    if (root/'en.json').is_file(): catalog_dir=root
    elif (root/'src/locales/en.json').is_file(): catalog_dir=root/'src/locales'
    else: print('FAIL: could not find en.json in root or root/src/locales'); return 1
    en=load(catalog_dir/'en.json'); base=en['messages']; keys=set(base); errors=0
    candidates=[root/'translation_inventory.csv', catalog_dir/'translation_inventory.csv', catalog_dir.parent.parent/'localization'/'translation_inventory.csv', root/'localization'/'translation_inventory.csv']
    inv_path=next((p for p in candidates if p.is_file()),None)
    if inv_path is None: print('FAIL: missing translation_inventory.csv; checked '+', '.join(str(p) for p in candidates)); return 1
    with inv_path.open(encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
    bykey={r['key']:r for r in rows}
    # When the frozen metadata is present, verify its artifact hashes rather than trusting stale report text.
    metadata_dir = inv_path.parent
    report_path = metadata_dir / 'extraction_report.json'
    map_path = metadata_dir / 'source_to_key_map.json'
    migration_path = metadata_dir / 'key_migration_report.json'
    if report_path.exists() or map_path.exists() or migration_path.exists():
        missing = [p.name for p in (report_path, map_path, migration_path) if not p.is_file()]
        if missing:
            print('FAIL: extraction report, source-to-key map, and key-migration report must all be present for metadata hash validation; missing=' + ', '.join(missing)); errors = 1
        else:
            try:
                report = json.loads(report_path.read_text(encoding='utf-8'))
                digests = {
                    'englishCatalogSha256': hashlib.sha256((catalog_dir / 'en.json').read_bytes()).hexdigest(),
                    'inventorySha256': hashlib.sha256(inv_path.read_bytes()).hexdigest(),
                    'sourceMapSha256': hashlib.sha256(map_path.read_bytes()).hexdigest(),
                    'keyMigrationReportSha256': hashlib.sha256(migration_path.read_bytes()).hexdigest(),
                }
                for field, actual_digest in digests.items():
                    if report.get(field) != actual_digest:
                        print(f'FAIL: extraction report {field} mismatch; expected={actual_digest}; recorded={report.get(field)}')
                        errors += 1
            except Exception as e:
                print(f'FAIL: extraction report metadata invalid: {e}'); errors += 1
    if set(bykey)!=keys or len(rows)!=len(keys): print('FAIL: inventory/catalog key parity mismatch'); errors+=1
    for k,v in base.items():
        if not isinstance(v,str) or not v.strip(): print(f'FAIL: empty English value: {k}'); errors+=1
    for k,r in bykey.items():
        if r.get('classification') not in ('TRANSLATE','KEEP_ENGLISH'): print(f'FAIL: unresolved classification: {k}'); errors+=1
        if k in base and r.get('english')!=base[k]: print(f'FAIL: inventory English mismatch: {k}'); errors+=1
        if k in base:
            # Inventory metadata is part of the localization contract, not a loose note.
            # It must contain exactly the distinct named placeholders found in English.
            expected_placeholders = sorted(set(TOKEN.findall(base[k])))
            raw_placeholders = r.get('placeholders', '')
            recorded = [part.strip() for part in raw_placeholders.split(';') if part.strip()]
            if recorded != sorted(set(recorded)) or recorded != expected_placeholders:
                print(f'FAIL: inventory placeholder field mismatch: {k}; expected={expected_placeholders}; recorded={recorded}')
                errors += 1
            if r.get('classification')=='TRANSLATE' and re.search(r'\{arg\d+\}',base[k]) and not r.get('placeholder_notes','').strip():
                print(f'FAIL: translated positional placeholder lacks semantic note: {k}'); errors+=1
    blocked_tokens = ('PID(s)', 'process(es)', 'candidate(s)', 'item(s)')
    for k in ('msg.tcp_port_listener_pid_s_arg1_arg2.41f3d3b1','msg.backend_pid_stopped_pid_was_stopped_but_port.9c4659af','msg.stop_could_not_verify_port_port_is_free_list.5fded1ba','msg.stop_did_not_complete_verified_rdt_connect_b.c114a91c','msg.phase_backend_candidate_arg1_additional_cand.87545f12'):
        if k not in base: print(f'FAIL: required audited message missing: {k}'); errors+=1; continue
        if any(token in base[k] for token in blocked_tokens): print(f'FAIL: pseudo-plural remains in audited message {k}'); errors+=1
        if re.search(r'\{arg\d+\}',base[k]): print(f'FAIL: generic positional placeholder remains in audited message {k}'); errors+=1
    semantic_requirements = {
        'msg.phase_snapshot_manager_pid_arg1_requested_ba.954482f0': {'manager_pid', 'requested_backend_pid', 'phase'},
        'msg.phase_manager_process_pid_arg1_ppid_arg2_pgi.fb87c089': {'manager_pid', 'parent_pid', 'process_group_id', 'process_state', 'command', 'phase'},
        'msg.phase_manager_process_pid_arg1_not_found_in.dd6c1a15': {'manager_pid', 'phase'},
        'msg.phase_port_port_listeners_arg1_safety.fce5d4d0': {'listener_descriptions', 'phase', 'port', 'safety'},
    }
    for k, required in semantic_requirements.items():
        if k not in base or k not in bykey:
            print(f'FAIL: audited semantic-placeholder key missing: {k}'); errors += 1; continue
        actual=set(TOKEN.findall(base[k]))
        if actual != required:
            print(f'FAIL: semantic placeholder contract mismatch {k}; expected={sorted(required)} actual={sorted(actual)}'); errors += 1
        if re.search(r'\{arg\d+\}',base[k]):
            print(f'FAIL: generic positional placeholder remains in raw diagnostic {k}'); errors += 1
        placeholder_notes=bykey[k].get('placeholder_notes','').strip()
        if not placeholder_notes:
            print(f'FAIL: missing semantic placeholder notes for {k}'); errors += 1
        for token in required:
            if '{'+token+'}' not in placeholder_notes:
                print(f'FAIL: placeholder notes do not document {{{token}}} for {k}'); errors += 1
    expected_classes = {
        'msg.phase_backend_candidate_arg1_additional_cand.87545f12':'KEEP_ENGLISH',
        'msg.phase_backend_candidate_pid_arg1_ppid_arg2_p.f19f073f':'KEEP_ENGLISH',
        'msg.phase_manager_process_pid_arg1_ppid_arg2_pgi.fb87c089':'KEEP_ENGLISH',
        'msg.phase_manager_process_pid_arg1_not_found_in.dd6c1a15':'KEEP_ENGLISH',
        'msg.phase_port_port_listeners_arg1_safety.fce5d4d0':'KEEP_ENGLISH',
        'msg.phase_snapshot_manager_pid_arg1_requested_ba.954482f0':'KEEP_ENGLISH'
    }
    for k,expected_class in expected_classes.items():
        if bykey.get(k,{}).get('classification')!=expected_class: print(f'FAIL: classification mismatch {k}; expected {expected_class}'); errors+=1
    if args.english_only:
        expected={'TRANSLATE':759,'KEEP_ENGLISH':122}
        actual={}
        for row in rows: actual[row['classification']]=actual.get(row['classification'],0)+1
        if actual!=expected: print(f'FAIL: classification totals {actual}; expected {expected}'); errors+=1
        if errors: print(f'FAIL: {errors} English catalog/inventory error(s)'); return 1
        print(f'PASS: English catalog and inventory agree on {len(keys)} keys; counts={actual}; no unresolved classifications.')
        return 0
    present=[]; missing=[]
    for code in CODES:
        p=catalog_dir/f'{code}.json'
        if not p.exists():
            if code!='en': missing.append(code)
            if code!='en' and not args.allow_partial: print(f'MISSING: {p.name}'); errors+=1
            continue
        try: d=load(p)
        except ValueError as e: print('FAIL:',e); errors+=1; continue
        present.append(code)
        if code=='en': continue
        meta=d.get('_meta',{})
        if meta.get('locale')!=code: print(f'FAIL {code}: locale metadata'); errors+=1
        if meta.get('direction')!=('rtl' if code in RTL else 'ltr'): print(f'FAIL {code}: text direction'); errors+=1
        if meta.get('catalogVersion')!=en.get('_meta',{}).get('catalogVersion'): print(f'FAIL {code}: catalogVersion'); errors+=1
        if not isinstance(meta.get('languageName'),str) or not meta['languageName'].strip(): print(f'FAIL {code}: languageName'); errors+=1
        if meta.get('source')!=en.get('_meta',{}).get('source'): print(f'FAIL {code}: source metadata'); errors+=1
        msgs=d['messages']; miss=keys-set(msgs); extra=set(msgs)-keys
        if miss: print(f'FAIL {code}: {len(miss)} missing keys; sample={sorted(miss)[:4]}'); errors+=1
        if extra: print(f'FAIL {code}: {len(extra)} extra keys; sample={sorted(extra)[:4]}'); errors+=1
        for k in keys & set(msgs):
            v=msgs[k]
            if not isinstance(v,str) or not v.strip(): print(f'FAIL {code}: empty/non-string {k}'); errors+=1; continue
            if sorted(TOKEN.findall(base[k]))!=sorted(TOKEN.findall(v)): print(f'FAIL {code}: placeholder mismatch {k}'); errors+=1
            if bykey.get(k,{}).get('classification')=='KEEP_ENGLISH' and v!=base[k]: print(f'FAIL {code}: KEEP_ENGLISH changed {k}'); errors+=1
    if args.allow_partial and len(present)<=1: print('FAIL: no translated locale files supplied'); errors+=1
    if errors: print(f'FAIL: {errors} validation errors'); return 1
    if missing and args.allow_partial: print(f'PASS: {len(present)-1} locale(s) valid; awaiting {", ".join(missing)}')
    else: print('PASS: all 26 catalogs valid, key/placeholder parity passes, KEEP_ENGLISH values preserved')
    return 0
if __name__=='__main__': raise SystemExit(main())
