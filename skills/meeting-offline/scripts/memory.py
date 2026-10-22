#!/usr/bin/env python3
"""Local confirmed meeting memory. No network calls; excluded from skill exports.

Role entry JSON:
{"id":"retail-ops-team-a","entity_type":"role","name":"零售运营负责人",
 "scope":"团队或组织","aliases":[],"responsibilities":[],
 "distinctive_signals":[],"confirmation":"用户在本次对话明确确认的依据"}
Term entry JSON:
{"id":"example-product","canonical":"标准写法","scope":"业务/组织",
 "aliases":["已确认的误识别"],"definition":"简要含义",
 "confirmation":"用户纠正原词并确认标准写法的依据"}

Add active:false to retire an entry. Upserts retain prior versions in history.
"""
import argparse
from contextlib import nullcontext
from file_lock import locked_file
import device_context
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STORE = device_context.paths()['memory']


def empty_store():
    return {'schema_version': 1, 'device_id': device_context.device_id(), 'roles': [], 'terms': [], 'history': []}


def read_store(path):
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else empty_store()
    if data.get('schema_version') != 1 or any(not isinstance(data.get(k), list) for k in ('roles', 'terms', 'history')):
        raise ValueError('Unsupported or invalid memory schema; existing file unchanged.')
    device_context.require_device(data.get('device_id'), path)
    data.setdefault('device_id', device_context.device_id())
    return data


def validate_entry(kind, entry):
    if not isinstance(entry, dict):
        raise ValueError('Entry must be an object.')
    if not isinstance(entry.get('id'), str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', entry['id']):
        raise ValueError('Entry ID must use lowercase letters, digits, and single hyphens.')
    required = ['scope', 'confirmation', 'name'] if kind == 'role' else ['scope', 'confirmation', 'canonical']
    for field in required:
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise ValueError('Missing nonempty string: ' + field)
    if kind == 'role' and 'role_title' in entry and not isinstance(entry['role_title'], str):
        raise ValueError('role_title must be a string.')
    if kind == 'role' and entry.get('entity_type') not in ('person', 'role'):
        raise ValueError('Role entity_type must be person or role.')
    for field in ('aliases', 'responsibilities', 'distinctive_signals'):
        if field in entry and (not isinstance(entry[field], list) or any(not isinstance(s, str) for s in entry[field])):
            raise ValueError(field + ' must be a list of strings.')
    if 'revision' in entry and (type(entry['revision']) is not int or entry['revision'] < 1):
        raise ValueError('revision must be a positive integer.')
    if 'active' in entry and type(entry['active']) is not bool:
        raise ValueError('active must be boolean.')


def atomic_write(path, data):
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='.meeting-memory-', dir=str(path.parent))
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def upsert_confirmed(path, kind, entry, *, _lock_held=False):
    """Shared versioned writer; caller must establish explicit user confirmation.

    _lock_held is for workspace transactions holding the same memory lock.
    """
    path = Path(path)
    validate_entry(kind, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with nullcontext() if _lock_held else locked_file(str(path) + '.lock'):
        data = read_store(path)
        bucket = kind + 's'
        prior = next((e for e in data[bucket] if e['id'] == entry['id']), None)
        now = datetime.now(timezone.utc).isoformat()
        if prior:
            if 'revision' in prior and (type(prior['revision']) is not int or prior['revision'] < 1):
                raise ValueError('revision must be a positive integer.')
            data['history'].append({'kind': kind, 'replaced_at': now, 'previous': deepcopy(prior)})
            # Preserve established aliases and other fields unless explicitly replaced.
            new_entry = {**prior, **entry}
        else:
            new_entry = dict(entry)
        new_entry.update({'confirmed_by_user': True, 'updated_at': now,
                          'created_at': prior.get('created_at', now) if prior else now,
                          'revision': prior.get('revision', 1) + 1 if prior else 1})
        new_entry.setdefault('active', True)
        data[bucket] = [e for e in data[bucket] if e['id'] != entry['id']] + [new_entry]
        atomic_write(path, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--store', type=Path, default=DEFAULT_STORE)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    show = sub.add_parser('show')
    show.add_argument('--include-history', action='store_true')
    update = sub.add_parser('upsert')
    update.add_argument('--kind', choices=('role', 'term'), required=True)
    update.add_argument('--entry', type=Path, required=True)
    update.add_argument('--user-confirmed', action='store_true')
    args = parser.parse_args()
    path = args.store.expanduser()
    try:
        if args.command == 'show':
            data = read_store(path)
            if not args.include_history:
                data = {k: v for k, v in data.items() if k != 'history'}
                for key in ('roles', 'terms'):
                    data[key] = [e for e in data[key] if e.get('active', True)]
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        if args.command == 'upsert' and not args.user_confirmed:
            raise ValueError('Only user-confirmed records may enter persistent memory.')
        entry = None
        if args.command == 'upsert':
            entry = json.loads(args.entry.read_text(encoding='utf-8'))
            validate_entry(args.kind, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_file(str(path) + '.lock'):
            data = read_store(path)
            if args.command == 'init':
                if not path.exists():
                    atomic_write(path, data)
                print('Local memory ready: ' + str(path))
                return
            upsert_confirmed(path, args.kind, entry, _lock_held=True)
            print('Saved confirmed ' + args.kind + ': ' + entry['id'])
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
