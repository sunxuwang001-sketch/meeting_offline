#!/usr/bin/env python3
"""Local meeting workspace, role review, scoped corrections and reproducible exports."""
from __future__ import annotations
import argparse
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from file_lock import locked_file
import device_context
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid
from html.parser import HTMLParser
import memory
import minutes_html
import minutes_md
import terms


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    memory.atomic_write(path, value)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for part in iter(lambda: handle.read(1048576), b''):
            h.update(part)
    return h.hexdigest()


def timestamp(value, srt=False):
    value = int(value or 0)
    h, value = divmod(value, 3600000); m, value = divmod(value, 60000); s, ms = divmod(value, 1000)
    return f'{h:02}:{m:02}:{s:02}' + (f',{ms:03}' if srt else '')


@contextmanager
def locked(root):
    root = Path(root)
    with locked_file(root / '.workspace.lock'):
        if (root / 'state.json').exists():
            assert_workspace(root, read(root / 'state.json'))
        yield


def assert_workspace(root, state):
    device_context.require_device(state.get('device_id'), root)


def pairs(values):
    result = {}
    for value in values or []:
        key, sep, path = value.partition('=')
        if not sep or key in result:
            raise ValueError('Expected unique ID=PATH entries.')
        result[key] = Path(path).expanduser().resolve()
    return result


def initialize(state_path, root, audio_paths):
    root = Path(root).expanduser().resolve()
    if root.exists():
        raise ValueError('Workspace exists; reuse it instead of overwriting confirmed roles.')
    state_path = Path(state_path).expanduser().resolve()
    state = read(state_path)
    state = deepcopy(state)
    if state.get('device_id'):
        device_context.require_device(state['device_id'], state_path)
    state['device_id'] = device_context.device_id()
    ids = [s['source_id'] for s in state['sources']]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r'[a-zA-Z0-9_-]+', i) for i in ids):
        raise ValueError('Source IDs must be unique letters, digits, hyphens or underscores.')
    if set(audio_paths) - set(ids):
        raise ValueError('Audio source not present in the state.')
    prepared = []
    for source in state['sources']:
        path = Path(source['path'])
        if not path.is_absolute():
            path = (state_path.parent / path).resolve()
        transcript = read(path)
        if not transcript.get('segments'):
            raise ValueError('Source has no segments: ' + source['source_id'])
        audio = audio_paths.get(source['source_id'])
        if audio and not audio.is_file():
            raise ValueError('Audio does not exist: ' + str(audio))
        prepared.append((source, path, transcript, audio))
    root.mkdir(parents=True, mode=0o700)
    for source, path, transcript, audio in prepared:
        local = root / 'transcripts' / (source['source_id'] + '.json')
        write(local, transcript)
        source.update(original_transcript_path=str(path), path=str(local),
                      transcript_sha256=file_digest(local),
                      audio_path=str(audio) if audio else None,
                      audio_sha256=file_digest(audio) if audio else None,
                      title=audio.name if audio else path.name)
    state['workspace_id'] = uuid.uuid4().hex
    state['schema_version'] = 2
    state['revision'] = 1
    state['created_at'] = now()
    state['pending_role_keys'] = [s['key'] for s in state['speakers'] if not s.get('role_confirmed') and not s.get('ignored_by_user')]
    state['status'] = 'awaiting_roles' if requires_role_review(state) and state['pending_role_keys'] else 'ready_for_minutes'
    write(root / 'state.json', state)
    write(root / 'corrections.json', [])
    write(root / 'analysis.json', {'sources': {}})
    write(root / 'manifest.json', {'schema_version': 2, 'revision': 1,
        'status': 'local_transcript_imported', 'created_at': now(),
        'network_calls': 0, 'audio_copied': False})
    import voiceprint
    try:
        state['voiceprint_match'] = voiceprint.match_workspace(root, state=state)
    except ValueError as error:
        state['voiceprint_match'] = {'enabled': True, 'error': str(error)}
    state['status'] = 'awaiting_roles' if requires_role_review(state) and state['pending_role_keys'] else 'ready_for_minutes'
    write(root / 'state.json', state)
    snapshot(root)
    export(root)
    return root


def requires_role_review(state, source_id=None):
    """Each recording retains explicit review when sandboxed or opt-in matching found nobody."""
    if device_context.sandbox():
        return True
    voice = device_context.minutes_preferences().get('voiceprint', {})
    if not isinstance(voice, dict) or voice.get('enabled') is not True:
        return False
    if source_id is None:
        return any(requires_role_review(state, source['source_id']) for source in state.get('sources', []))
    speakers = [speaker for speaker in state.get('speakers', []) if speaker['source_id'] == source_id]
    matches = state.get('voiceprint_match', {}).get('matches', {})
    return not any(s.get('role_source') == 'voice_match' or s['key'] in matches for s in speakers)


def speaker_label(speaker):
    if speaker.get('role_confirmed') or speaker.get('role_source') == 'voice_match':
        return speaker.get('role_name') or speaker.get('display_name') or speaker['key']
    if speaker.get('diarization_unassigned') or speaker['key'].endswith('-UNASSIGNED'):
        return '说话人未分离'
    return '说话人 ' + speaker['key'].rsplit('-', 1)[-1]


def workspace_identity(root, state):
    return state.get('workspace_id') or digest([str(Path(root).resolve()), state['device_id'], state.get('created_at')])


def snapshot(root):
    state = read(root / 'state.json')
    write(root / 'revisions' / f"{state['revision']:06}.json", {
        'state': state, 'corrections': read(root / 'corrections.json'),
        'analysis': read(root / 'analysis.json')})


def memory_path(state):
    path = Path(state.get('memory_store') or memory.DEFAULT_STORE).expanduser()
    device_context.require_device(state.get('device_id'), path)
    return path


def commit(root, state, change):
    assert_workspace(root, state)
    validate_analysis(root, read(root / 'analysis.json'), state=state)
    state['device_id'] = device_context.device_id()
    state['revision'] += 1
    state['updated_at'] = now()
    state.setdefault('changes', []).append({'at': now(), **change})
    state['pending_role_keys'] = [s['key'] for s in state['speakers'] if not s.get('role_confirmed') and not s.get('ignored_by_user')]
    state['status'] = 'awaiting_roles' if requires_role_review(state) and state['pending_role_keys'] else 'ready_for_minutes'
    write(root / 'state.json', state)
    manifest = read(root / 'manifest.json')
    manifest.update(revision=state['revision'], status=state['status'], updated_at=now())
    write(root / 'manifest.json', manifest)
    snapshot(root)
    return export(root)



def restore_bytes(backups):
    """Restore exact snapshots independently of the failing JSON writer."""
    for path, content in backups.items():
        if content is None:
            path.unlink(missing_ok=True)
            continue
        fd, temporary = tempfile.mkstemp(prefix='.rollback-', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)


def commit_analysis(root, state, change, analysis):
    """Commit analysis and its state under the caller's workspace lock; roll back failed writes."""
    root = Path(root)
    paths = [root / name for name in ('analysis.json', 'state.json', 'manifest.json', 'exports/latest.json')]
    paths.append(root / 'revisions' / f"{state['revision'] + 1:06}.json")
    backups = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        write(root / 'analysis.json', analysis)
        return commit(root, state, change)
    except Exception:
        restore_bytes(backups)
        raise


def remaining_old_names(prose, old_names, current_names):
    # Match the longest identity token once: protect new extensions, retain old longer names.
    tokens = sorted({name for name in list(old_names) + list(current_names) if name}, key=len, reverse=True)
    if not tokens:
        return []
    found = re.findall('|'.join(re.escape(name) for name in tokens), prose)
    return sorted(set(found).intersection(old_names))


def analysis_prose(section):
    """Only authored prose, never source evidence, speaker keys, or attribution metadata."""
    values = [section.get('title', '')]
    reader = section.get('reader', {})
    values.extend(reader.get(field, '') for field in ('summary', 'topic'))
    for field, texts in (('points', ('title', 'text')), ('todos', ('task', 'deadline'))):
        for item in reader.get(field, []):
            values.extend(item.get(key, '') for key in texts)
    for field, texts in (('decisions', ('text',)), ('actions', ('task', 'deliverable', 'deadline_text')),
                         ('open_questions', ('text',))):
        for item in section.get(field, []):
            values.extend(item.get(key, '') for key in texts)
    return '\n'.join(value for value in values if isinstance(value, str))


def check_revision(state, expected):
    if type(expected) is not int or expected != state['revision']:
        raise ValueError('页面内容已更新，请刷新后再保存。')


def confirmed_memory_upsert(store, kind, entry, *, _lock_held=False):
    # The CLI and workspace share one locked, versioned writer.
    memory.upsert_confirmed(store, kind, entry, _lock_held=_lock_held)


@contextmanager
def identity_store_transaction(state):
    """Hold shared-store locks through workspace commit and rollback failed writes.

    Lock order: workspace, memory, voiceprint. Never restore an unlocked whole
    store: another meeting may be updating a different profile concurrently.
    """
    import voiceprint
    store, library = memory_path(state), voiceprint.location()
    store.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        locks.enter_context(locked_file(str(store) + '.lock'))
        # Disabled installations with no library must remain usable without
        # creating the optional biometric store (or its parent directory).
        voice_active = library.exists() or voiceprint.status()['enabled']
        paths = [store]
        if voice_active:
            library.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            locks.enter_context(locked_file(library.with_suffix('.lock')))
            paths.append(library)
        backups = {path: path.read_bytes() if path.exists() else None for path in paths}
        try:
            yield voice_active
        except Exception:
            restore_bytes(backups)
            raise


def save_roles(root, mappings, expected, via='page'):
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); check_revision(state, expected)
        with identity_store_transaction(state) as voice_active:
            known = memory.read_store(memory_path(state))
            profiles = {p['id']: p for p in known['roles'] if p.get('active', True)}
            speakers = {s['key']: s for s in state['speakers']}
            if not isinstance(mappings, list) or any(not isinstance(m, dict) or not isinstance(m.get('key'), str) for m in mappings) \
                    or len({m['key'] for m in mappings}) != len(mappings):
                raise ValueError('Invalid or duplicate speaker mappings.')
            old_speakers = deepcopy(speakers)
            resolved = []
            new_profiles = []
            reconfirmed_profiles = []
            enrollment_keys = []
            for mapping in mappings:
                if mapping['key'] not in speakers:
                    raise ValueError('Unknown speaker.')
                if mapping.get('keep_unknown'):
                    resolved.append((mapping['key'], None)); continue
                if speakers[mapping['key']].get('diarization_unassigned') and mapping.get('confirm_single_speaker') is not True:
                    # Speech the separation could not attribute may mix several people.
                    raise ValueError('“说话人未分离”的部分可能混有多人的发言，不能直接认领成一个人。'
                                     '确定这部分都是同一个人说的，请在复核页面勾选“这部分都是同一个人说的”'
                                     '（在对话里确认的，映射里加 confirm_single_speaker）；否则选“明确保留身份未确认”。')
                profile_id = mapping.get('profile_id')
                if profile_id:
                    if profile_id not in profiles:
                        raise ValueError('Unknown/inactive profile.')
                    profile = profiles[profile_id]
                    original = old_speakers[mapping['key']]
                    explicit = (mapping.get('confirm_identity') is True or not original.get('role_confirmed')
                                or original.get('identity_group') != profile_id)
                    if explicit and (profile.get('confirmed_by_user') is not True or not profile.get('confirmation')):
                        # A current explicit selection upgrades only this legacy profile.
                        profile = {**profile, 'confirmation': ({
                            'page': '用户在本地说话人复核页明确选择已有档案并保存：',
                            'html': '用户在纪要网页中明确确认已有档案：'
                        }.get(via, '用户在对话中明确确认已有档案：')) + mapping['key']}
                        reconfirmed_profiles.append(profile)
                    if explicit:
                        # Only an upgraded or newly chosen profile is written back; check it first.
                        try:
                            memory.validate_entry('role', profile)
                        except ValueError as error:
                            raise ValueError(f'已有档案“{profile.get("name", profile_id)}”的记录不完整（{error}），'
                                             '不能用来确认身份。请改为填写新的姓名，或先修好这条档案。') from error
                        enrollment_keys.append(mapping['key'])
                    elif not isinstance(profile.get('name'), str) or not profile['name'].strip():
                        raise ValueError(f'已有档案 {profile_id} 缺少姓名；请勾选确认后重新填写这位说话人。')
                else:
                    name = str(mapping.get('name', '')).strip()
                    role = str(mapping.get('role_title', '')).strip()
                    scope = str(mapping.get('scope', '')).strip() or '本次会议，组织待补'
                    if not name or len(name) > 100 or len(role) > 200:
                        raise ValueError('请填写姓名或角色称呼。')
                    entity_type = mapping.get('entity_type', 'role')
                    if entity_type not in ('person', 'role'):
                        raise ValueError('请选择具体人物或仅角色称呼。')
                    profile_id = entity_type + '-' + hashlib.sha256((scope + '\0' + name).encode()).hexdigest()[:16]
                    profile = profiles.get(profile_id) or {
                        'id': profile_id, 'entity_type': entity_type, 'name': name,
                        'scope': scope, 'role_title': role, 'aliases': [],
                        'confirmation': ({'page': '用户在本地说话人复核页明确填写并保存：', 'html': '用户在纪要网页中修改并确认：'}.get(via, '用户在对话中明确确认：')) + mapping['key']}
                    new_profiles.append(profile)
                    enrollment_keys.append(mapping['key'])
                resolved.append((mapping['key'], profile))
            for key, profile in resolved:
                speaker = speakers[key]
                if profile is None:
                    speaker.update(role_name=minutes_html.UNKNOWN_ROLE, display_name=None, identity_group=None, accepted_unknown=True)
                else:
                    speaker.update(role_name=profile['name'] + ('｜' + profile['role_title'] if profile.get('role_title') else ''),
                                   display_name=profile['name'], identity_group=profile['id'], accepted_unknown=False)
                speaker['role_confirmed'] = True
                speaker['role_source'] = 'user'
            # Identity changes must update authored prose regardless of confirmation entry point.
            # Include keep_unknown: withdrawing an identity must not leave publishable named prose.
            updated_analysis = sync_reader_names(root, state, old_speakers, [key for key, _ in resolved])
            try:
                validate_analysis(root, updated_analysis if updated_analysis is not None else read(root / 'analysis.json'), state=state)
            except ValueError as error:
                raise ValueError('角色更新与现有纪要归属冲突，尚未保存。请先修改相关负责人或提出者归属后重试：' + str(error)) from error
            for profile in {p['id']: p for p in reconfirmed_profiles}.values():
                confirmed_memory_upsert(memory_path(state), 'role', profile, _lock_held=True)
            for profile in new_profiles:
                confirmed_memory_upsert(memory_path(state), 'role', profile, _lock_held=True)
                if profile['entity_type'] == 'person':
                    confirmed_memory_upsert(memory_path(state), 'term', {
                        'id': 'name-' + digest([profile['scope'], profile['name']])[:16],
                        'canonical': profile['name'], 'scope': profile['scope'], 'aliases': [],
                        'definition': profile.get('role_title', ''),
                        'confirmation': {'page': '用户在本地角色复核页明确选择具体人物并填写姓名。', 'html': '用户在纪要网页中修改并确认'}.get(via, '用户在对话中明确确认具体人物的姓名。')}, _lock_held=True)
            import voiceprint
            changed_keys = [key for key, _ in resolved if any(
                (old_speakers[key].get(field) if field == 'identity_group' else bool(old_speakers[key].get(field))) !=
                (speakers[key].get(field) if field == 'identity_group' else bool(speakers[key].get(field)))
                for field in ('identity_group', 'role_confirmed', 'accepted_unknown'))]
            state.pop('voiceprint_enrollment_warning', None)
            try:
                if voice_active:
                    voiceprint.enroll_confirmed(root, state, enrollment_keys,
                                                _lock_held=True, _reconcile_keys=changed_keys)
            except voiceprint.ReconciliationError:
                raise  # A failed withdrawal must never be reported as saved.
            except (ValueError, RuntimeError, OSError, ImportError) as error:
                state['voiceprint_enrollment_warning'] = str(error)
            change = {'kind': 'roles_confirmed', 'speaker_keys': [m[0] for m in resolved], 'actor': 'user'}
            return commit_analysis(root, state, change, updated_analysis)

def sync_reader_names(root, state, old_speakers, keys):
    """Apply the same conservative single-pass rename as HTML; withhold unsafe stale prose."""
    analysis = read(Path(root) / 'analysis.json')
    speakers = {s['key']: s for s in state['speakers']}
    for sid in {speakers[k]['source_id'] for k in keys}:
        mappings = {}
        aliases = []
        old_names = [minutes_html.shown_name(old, display_names()) for old in old_speakers.values() if old['source_id'] == sid]
        ambiguous = {name for name in old_names if old_names.count(name) > 1}
        for key, old in old_speakers.items():
            if old['source_id'] != sid:
                continue
            old_name = minutes_html.shown_name(old, display_names())
            aliases.extend([key, old_name])
            if key in keys:
                new_name = minutes_html.shown_name(speakers[key], display_names())
                mappings.update({a: new_name for a in (key, old_name) if a != new_name and a not in ambiguous})
        section = analysis.get('sources', {}).get(sid, {})
        old_tokens = set(state.get('reader_stale_sources', {}).get(sid, {}).get('old_names', []))
        current_names = [minutes_html.shown_name(s, display_names()) for s in speakers.values() if s['source_id'] == sid]
        for key in keys:
            if old_speakers[key]['source_id'] == sid:
                before = minutes_html.shown_name(old_speakers[key], display_names())
                after = minutes_html.shown_name(speakers[key], display_names())
                if before != after: old_tokens.update((key, before))
        remaining = set()
        def replace(value):
            result = minutes_html.replace_speaker_names(value, mappings, aliases)
            # No guessing when a name sits inside ambiguous text or is a single character.
            remaining.update(remaining_old_names(result, old_tokens, current_names))
            return result
        if isinstance(section.get('title'), str): section['title'] = replace(section['title'])
        for speaker in state['speakers']:
            if speaker['source_id'] != sid:
                continue
            for index, point in enumerate(speaker.get('core_points', [])):
                if isinstance(point, dict) and isinstance(point.get('text'), str):
                    point['text'] = replace(point['text'])
                elif isinstance(point, str):
                    speaker['core_points'][index] = replace(point)
        reader = section.get('reader', {})
        for field in ('summary', 'topic'):
            if isinstance(reader.get(field), str): reader[field] = replace(reader[field])
        for field, texts in (('points', ('title', 'text')), ('todos', ('task', 'deadline'))):
            for item in reader.get(field, []):
                for name in texts:
                    if isinstance(item.get(name), str): item[name] = replace(item[name])
        for field, text_fields in (('decisions', ('text',)), ('actions', ('task', 'deliverable', 'deadline_text')), ('open_questions', ('text',))):
            for item in section.get(field, []):
                for text_field in text_fields:
                    if isinstance(item.get(text_field), str): item[text_field] = replace(item[text_field])
        remaining.update(remaining_old_names(analysis_prose(section), old_tokens, current_names))
        if remaining or sid in state.get('reader_stale_sources', {}):
            state.setdefault('reader_stale_sources', {})[sid] = {
                'reason': 'speaker_rename_requires_prose_review', 'old_names': sorted(old_tokens),
                'remaining_old_names': sorted(remaining)}
    return analysis


def add_term(root, payload, expected):
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); check_revision(state, expected)
        sources = {s['source_id']: s for s in state['sources']}
        source_id = payload.get('source_id')
        if source_id not in sources:
            raise ValueError('请选择本次要校对的录音。')
        old, canonical = str(payload.get('heard', '')).strip(), str(payload.get('canonical', '')).strip()
        scope = str(payload.get('scope', '')).strip()
        if not old or not canonical or not scope or old == canonical or max(len(old), len(canonical)) > 100:
            raise ValueError('请填写不同的原识别、标准写法和适用范围（每个词不超过100字）。')
        store = memory.read_store(memory_path(state))
        for term in store['terms']:
            if term.get('active', True) and term['scope'] == scope and old in term.get('aliases', []) and term['canonical'] != canonical:
                raise ValueError('此误识别形式在相同范围已有另一种标准写法，请先明确歧义。')
        existing = next((t for t in store['terms'] if t['scope'] == scope and t['canonical'] == canonical and t.get('active', True)), None)
        term = {**(existing or {}), 'id': existing['id'] if existing else 'term-' + digest([scope, canonical])[:16],
                'canonical': canonical, 'scope': scope,
                'aliases': sorted(set((existing or {}).get('aliases', []) + [old])),
                'confirmation': '用户在本地术语复核页明确确认：' + old + ' → ' + canonical}
        corrections = read(root / 'corrections.json')
        transcript = read(sources[source_id]['path'])
        protected = terms.known_forms(corrections, state.get('unconfirmed_term_candidates', []), store['terms'])
        indices = [i for i, s in enumerate(transcript['segments'])
                   if terms.contains(corrected_text(s['text'], source_id, i, corrections, protected), old, canonical, protected)]
        if not indices:
            raise ValueError('当前录音中未找到该写法；未创建替换，请检查原词。')
        confirmed_memory_upsert(memory_path(state), 'term', term)
        correction = {'id': digest([source_id, old, canonical, state['revision']])[:16],
            'source_id': source_id, 'heard': old, 'canonical': canonical, 'scope': scope,
            'segment_indices': indices, 'active': True, 'confirmed_at': now(), 'term_id': term['id']}
        corrections.append(correction); write(root / 'corrections.json', corrections)
        return commit(root, state, {'kind': 'term_confirmed', 'correction_id': correction['id'], 'actor': 'user'})


def protected_forms(state, corrections):
    """Known spellings in this workspace and its device memory, so a short form never rewrites a longer one."""
    store = memory.read_store(memory_path(state))
    return terms.known_forms(corrections, state.get('unconfirmed_term_candidates', []), store['terms'])


def corrected_text(text, source_id, index, corrections, protected=None):
    if protected is None:
        protected = terms.known_forms(corrections)
    for correction in corrections:
        if correction.get('active', True) and correction['source_id'] == source_id and index in correction['segment_indices']:
            text = terms.replace(text, correction['heard'], correction['canonical'], protected)
    return text


def validate_analysis(root, analysis, state=None):
    """Check the assistant-written analysis against the transcript; errors name the offending item."""
    state = read(Path(root) / 'state.json') if state is None else state
    assert_workspace(root, state)
    sources = {s['source_id']: read(s['path']) for s in state['sources']}
    speakers = {s['key']: s for s in state['speakers']}
    if not isinstance(analysis, dict) or not isinstance(analysis.get('sources'), dict):
        raise ValueError('Analysis requires a sources object.')
    for source_id, section in analysis['sources'].items():
        if source_id not in sources:
            raise ValueError(f'Analysis references an unknown source: {source_id}.')
        if not isinstance(section, dict):
            raise ValueError(f'sources.{source_id} must be an object.')
        count = len(sources[source_id]['segments'])

        def check_refs(refs, where, what='valid zero-based segment evidence'):
            if not isinstance(refs, list) or not refs or any(type(i) is not int or not 0 <= i < count for i in refs):
                raise ValueError(f'{where}: requires {what} (segment indices 0–{count - 1}).')

        for field in ('decisions', 'actions', 'open_questions'):
            items = section.get(field, [])
            if not isinstance(items, list):
                raise ValueError(f'{source_id}.{field} must be a list.')
            for n, item in enumerate(items):
                where = f'{source_id}.{field}[{n}]'
                if not isinstance(item, dict):
                    raise ValueError(f'{where} must be an object.')
                check_refs(item.get('evidence'), where)
                for key in ('proposer', 'owner'):
                    if item.get(key) and (item[key] not in speakers or speakers[item[key]]['source_id'] != source_id):
                        raise ValueError(f'{where}.{key}: speaker attribution must reference this source.')
                    if item.get(key) and (speakers[item[key]].get('diarization_unassigned') or item[key].endswith('-UNASSIGNED')) and not speakers[item[key]].get('identity_group'):
                        raise ValueError(f'{where}.{key}: 来自“说话人未分离”的片段，不能当作某个人的发言或认领。')
                if field == 'decisions' and item.get('status') not in ('direction', 'proposal', 'decided', 'open'):
                    raise ValueError(f'{where}.status must be direction, proposal, decided, or open.')
                if field == 'actions' and item.get('owner') and item.get('owner_basis') not in ('self_commitment', 'explicit_assignment'):
                    raise ValueError(f'{where}: named action owners require explicit attribution evidence (owner_basis).')
        if 'reader' in section:
            reader = section['reader']
            if not isinstance(reader, dict) or not isinstance(reader.get('points', []), list) or not isinstance(reader.get('todos', []), list):
                raise ValueError(f'{source_id}.reader requires point and todo lists.')
            if 'topic' in reader and not isinstance(reader['topic'], str):
                raise ValueError(f'{source_id}.reader.topic must be text.')
            if 'summary' in reader:
                if not isinstance(reader['summary'], str) or not reader['summary'].strip():
                    raise ValueError(f'{source_id}.reader.summary must be nonempty text.')
                check_refs(reader.get('summary_evidence'), f'{source_id}.reader.summary_evidence', 'valid source evidence')
            for n, point in enumerate(reader.get('points', [])):
                where = f'{source_id}.reader.points[{n}]'
                if not isinstance(point, dict) or any(not isinstance(point.get(k), str) or not point[k].strip() for k in ('title', 'text')):
                    raise ValueError(f'{where} requires title and text.')
                check_refs(point.get('evidence'), where, 'valid source evidence')
                if 'status' in point and point['status'] not in minutes_html.STATUS:
                    raise ValueError(f'{where}.status must be decided, direction, proposal or open.')
                attribution = point.get('attribution', [])
                if not isinstance(attribution, list) or any(k not in speakers or speakers[k]['source_id'] != source_id for k in attribution):
                    raise ValueError(f'{where}.attribution must reference speakers of this source.')
            seen = set()
            for n, todo in enumerate(reader.get('todos', [])):
                where = f'{source_id}.reader.todos[{n}]'
                if not isinstance(todo, dict):
                    raise ValueError(f'{where} must be an object.')
                i = todo.get('action_index')
                if type(i) is not int or not 0 <= i < len(section.get('actions', [])) or i in seen:
                    raise ValueError(f'{where}.action_index must be a unique index into actions.')
                seen.add(i)
                if any(k in todo and (not isinstance(todo[k], str) or not todo[k].strip()) for k in ('task', 'deadline')) or set(todo) - {'action_index', 'task', 'deadline'}:
                    raise ValueError(f'{where}: may only shorten task/deadline text; ownership comes from the original action.')
    return analysis


def set_analysis(root, analysis, expected):
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); check_revision(state, expected)
        validate_analysis(root, analysis)
        for sid, section in analysis['sources'].items():
            stale = state.setdefault('reader_stale_sources', {}).get(sid)
            if stale is None:
                continue
            names = stale.get('old_names')
            # Legacy markers lacking names cannot silently authorize old prose.
            if not names:
                continue
            prose = analysis_prose(section)
            # Text minutes always include these cards; HTML also uses them as a fallback.
            for speaker in state['speakers']:
                if speaker['source_id'] == sid:
                    for point in speaker.get('core_points', []):
                        text = point.get('text', '') if isinstance(point, dict) else point
                        if isinstance(text, str): prose += '\n' + text
            current_names = [minutes_html.shown_name(s, display_names()) for s in state['speakers'] if s['source_id'] == sid]
            remaining = remaining_old_names(prose, names, current_names)
            if remaining:
                stale['remaining_old_names'] = remaining
            else:
                state['reader_stale_sources'].pop(sid, None)
        return commit_analysis(root, state, {'kind': 'analysis_updated', 'actor': 'assistant', 'semantic_review': 'pending'}, analysis)


def clips_for(speaker):
    turns = [u for u in speaker['utterances'] if type(u.get('start_ms')) in (int, float)
             and type(u.get('end_ms')) in (int, float) and u['end_ms'] - u['start_ms'] >= 3000 and len(u['text']) >= 10]
    if not turns:
        turns = [u for u in speaker['utterances'] if u.get('start_ms') is not None and u.get('end_ms') is not None]
    quotes = {q['text'] for q in speaker.get('recognition_quotes', [])}
    preferred = [u for u in turns if u['text'] in quotes]
    for index in (0, len(turns)//2, len(turns)-1):
        if turns and turns[index] not in preferred:
            preferred.append(turns[index])
    return [{'start_ms': u['start_ms'], 'end_ms': min(u['end_ms'], u['start_ms'] + 20000), 'text': u['text']} for u in preferred[:3]]


def term_preview(root, source_id, heard):
    state = read(Path(root) / 'state.json')
    source = next((s for s in state['sources'] if s['source_id'] == source_id), None)
    if not source or not heard or len(heard) > 100:
        return {'count': 0, 'matches': []}
    corrections = read(Path(root) / 'corrections.json')
    protected = protected_forms(state, corrections)
    matches = []
    for i, segment in enumerate(read(source['path'])['segments']):
        current = corrected_text(segment['text'], source_id, i, corrections, protected)
        if terms.contains(current, heard, None, protected):
            matches.append({'index': i, 'text': current, 'original_text': segment['text'],
                            'start_ms': segment['start_ms'], 'end_ms': min(segment['end_ms'], segment['start_ms'] + 20000)})
    return {'count': len(matches), 'matches': matches[:6]}


def public_state(root):
    import term_review
    root = Path(root); state = read(root / 'state.json')
    assert_workspace(root, state)
    mem = memory.read_store(memory_path(state))
    return {'revision': state['revision'], 'status': state['status'], 'identity_confirmation_required': requires_role_review(state),
        'sources': [{'source_id': s['source_id'], 'title': s['title'], 'has_audio': bool(s.get('audio_path'))} for s in state['sources']],
        'speakers': [{k: s.get(k) for k in ('key', 'source_id', 'role_name', 'role_confirmed', 'identity_group', 'focus', 'core_points', 'recognition_quotes', 'candidate_role', 'diarization_unassigned', 'role_source', 'voice_similarity', 'voice_profile_id')} | {'clips': clips_for(s)} for s in state['speakers']],
        'roles': [p for p in mem['roles'] if p.get('active', True)],
        'terms': [t for t in mem['terms'] if t.get('active', True)],
        'corrections': read(root / 'corrections.json'),
        'term_candidates': term_review.cards(root, state),
        'exports': read(root / 'exports/latest.json') if (root / 'exports/latest.json').exists() else None}


minutes_preferences = device_context.minutes_preferences


def preferred_theme():
    """preferences.meeting_offline.theme, one of minutes_html.THEMES; 暖白陶土 by default."""
    theme = minutes_preferences().get('theme', 'warm')
    if theme not in minutes_html.THEMES:
        raise ValueError('preferences 中的 theme 只能是 ' + '、'.join(minutes_html.THEMES) + '。')
    return theme


def preferred_layout():
    """preferences.meeting_offline.layout, one of minutes_html.LAYOUTS; 经典 by default."""
    layout = minutes_preferences().get('layout', 'classic')
    if layout not in minutes_html.LAYOUTS:
        raise ValueError('preferences 中的 layout 只能是 ' + '、'.join(minutes_html.LAYOUTS) + '。')
    return layout


def display_names():
    """Optional preferences.meeting_offline.display_names, e.g. {"用户本人": "我"}; empty by default."""
    names = minutes_preferences().get('display_names', {})
    if not isinstance(names, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in names.items()):
        raise ValueError('preferences 中的 display_names 必须是“已确认称呼 → 纪要显示名”的文本映射。')
    return names


def vault_links(state):
    """Confirmed participant name -> Obsidian note title, when a vault is set and has a note for them."""
    if not device_context.paths()['vault']:
        return {}
    import obsidian
    names = {s.get('display_name') or (s.get('role_name') or '').split('｜')[0] for s in state['speakers'] if s.get('role_confirmed')}
    return {name: note for name, note in obsidian.links().items() if name in names}


def export(root):
    root = Path(root); state = read(root / 'state.json')
    assert_workspace(root, state)
    analysis = validate_analysis(root, read(root / 'analysis.json'))
    corrections = read(root / 'corrections.json')
    protected = sorted(protected_forms(state, corrections))
    shown_names = display_names(); theme = preferred_theme(); layout = preferred_layout(); links = vault_links(state)
    private = read(root / 'insights.json') if (root / 'insights.json').exists() else {'sources': {}}
    signature = digest({'audit_issues': unresolved_audit(root), 'revision': state['revision'], 'theme': theme, 'layout': layout, 'insights': private,
                        'insights_renderer': file_digest(Path(__file__).with_name('insights.py')), 'speakers': state['speakers'], 'analysis': analysis, 'corrections': corrections, 'protected': protected,
                        'display_names': shown_names, 'vault_links': links,
                        'html_template': file_digest(minutes_html.TEMPLATE), 'html_renderer': file_digest(minutes_html.__file__),
                        'md_renderer': file_digest(minutes_md.__file__),
                        'audio': [Path(s['audio_path']).name if s.get('audio_path') else None for s in state['sources']],
                        'sources': [(s['source_id'], s['transcript_sha256']) for s in state['sources']]})[:16]
    directory = root / 'exports' / signature; directory.mkdir(parents=True, exist_ok=True)
    speakers = {s['key']: s for s in state['speakers']}
    output = {'version': signature, 'revision': state['revision'], 'generated_at': now(), 'files': {}}
    for source in state['sources']:
        sid = source['source_id']; transcript = read(source['path'])
        if file_digest(source['path']) != source['transcript_sha256']:
            raise ValueError('Immutable raw transcript changed; refusing to export.')
        names = {(type(s['source_speaker_id']).__name__, s['source_speaker_id']): speaker_label(s) for s in state['speakers'] if s['source_id'] == sid}
        lines = [source['title'] + '｜带角色转写', '自动识别身份待用户确认；原始转写单独保留。', '']
        subtitles = []; normalized = []
        for i, segment in enumerate(transcript['segments']):
            speaker_id = segment.get('speaker_id')
            name = names.get((type(speaker_id).__name__, speaker_id), '未分离')
            text = corrected_text(segment['text'], sid, i, corrections, protected)
            lines.append(f"[{timestamp(segment['start_ms'])}–{timestamp(segment['end_ms'])}] {name}\n{text}\n")
            subtitles.append(f"{i+1}\n{timestamp(segment['start_ms'], True)} --> {timestamp(segment['end_ms'], True)}\n{name}：{text}")
            normalized.append({**segment, 'speaker_name': name, 'text': text, 'original_text': segment['text'], 'segment_id': sid + ':' + str(i)})
        structured = deepcopy(analysis['sources'].get(sid, {}))
        def name(key):
            return speaker_label(speakers[key]) if key else '待明确'
        def evidence(refs):
            return [{'segment_id': sid + ':' + str(i), **transcript['segments'][i]} for i in refs]
        for field in ('decisions', 'actions', 'open_questions'):
            for claim in structured.get(field, []):
                text_key = 'task' if field == 'actions' else 'text'
                original = claim.get(text_key, '')
                revised = terms.correct_derived(original, sid, corrections, protected)
                if revised != original:
                    claim['original_analysis_text'] = original
                    claim[text_key] = revised
                claim['source_evidence'] = evidence(claim['evidence'])
                if claim.get('owner'): claim['owner_name'] = name(claim['owner'])
                if claim.get('proposer'): claim['proposer_name'] = name(claim['proposer'])
        confirmed = all(s.get('role_confirmed') or s.get('ignored_by_user') for s in state['speakers'] if s['source_id'] == sid)
        ready = (confirmed or not requires_role_review(state, sid)) and sid not in state.get('reader_stale_sources', {})
        minutes = [structured.get('title') or source['title'], '', '会议日期：' + str(structured.get('meeting_date') or '待确认'),
                   '状态：纪要由 AI 整理；自动识别姓名待确认，未识别者使用代号。' if ready else '状态：角色待确认，仅显示观点卡片。', '', '各角色核心观点']
        for speaker in state['speakers']:
            if speaker['source_id'] == sid and not speaker.get('ignored_by_user'):
                minutes.append('\n' + speaker_label(speaker))
                for point in speaker.get('core_points', []):
                    text = point.get('text', '') if isinstance(point, dict) else point
                    # Scoped user-approved term rules can normalize derived prose; raw evidence stays intact.
                    text = terms.correct_derived(text, sid, corrections, protected)
                    minutes.append('- ' + text)
        if ready:
            for field, title in (('decisions', '议题与决定状态'), ('actions', '行动项'), ('open_questions', '待确认事项')):
                minutes.extend(['', title])
                for i, claim in enumerate(structured.get(field, []), 1):
                    text = claim.get('text') or claim.get('task') or ''
                    minutes.append(str(i) + '. ' + text)
                    if field == 'decisions':
                        status = {'direction':'明确的推进方向', 'proposal':'建议或倾向', 'decided':'已决定', 'open':'未定'}[claim['status']]
                        minutes.append('状态：' + status + ('；提出者：' + name(claim.get('proposer')) if claim.get('proposer') else ''))
                    if field == 'actions':
                        minutes.extend(['负责人：' + name(claim.get('owner')), '交付物：' + (claim.get('deliverable') or '待明确'),
                            '期限：' + (claim.get('deadline_text') or '待明确') + ('（会议日期待确认，未换算）' if claim.get('deadline_text') and not structured.get('meeting_date') else '')])
                    for ref in claim['source_evidence']:
                        minutes.append('原文 [' + timestamp(ref['start_ms']) + '] ' + ref['text'])
        kinds = {'transcript': ('转写.txt', '\n'.join(lines)), 'subtitles': ('字幕.srt', '\n\n'.join(subtitles)+'\n'),
                 'minutes': ('纪要.txt', '\n'.join(minutes)+'\n')}
        files = {}
        for kind, (suffix, text) in kinds.items():
            path = directory / (sid + '-' + suffix)
            path.write_text(text, encoding='utf-8'); files[kind] = str(path)
        path = directory / (sid + '-structured.json')
        write(path, {'source_id': sid, 'roles_confirmed': confirmed, 'transcript': normalized, 'analysis': structured,
                     'semantic_review': 'pending', 'version': signature})
        files['structured'] = str(path)
        if ready and structured:
            document = minutes_html.from_workspace(state, sid, structured, corrections, protected, shown_names, theme, layout)
            document.update(workspace_id=workspace_identity(root, state), revision=state['revision'], source_id=sid)
            document['audit_issues'] = [p for p in unresolved_audit(root) if p['source_id'] == sid]
            path = directory / (sid + '-纪要.html')
            rendered = minutes_html.render(document)
            unchanged = path.exists() and path.read_text(encoding='utf-8') == rendered
            if not unchanged:
                path.write_text(rendered, encoding='utf-8')
            files['html'] = str(path)
            image = path.with_suffix('.png')
            png = {'png': str(image)} if unchanged and image.exists() else minutes_html.png_for(path, image)
            if png['png']:
                files['png'] = png['png']
            else:
                files['png_error'] = png['png_error']
            audio = Path(source['audio_path']).name if source.get('audio_path') else None
            meeting_date = structured.get('meeting_date')
            shown = {(type(s['source_speaker_id']).__name__, s['source_speaker_id']):
                     minutes_html.shown_name(s, shown_names) for s in state['speakers'] if s['source_id'] == sid}
            spoken = [{'speaker': shown.get((type(n.get('speaker_id')).__name__, n.get('speaker_id')), '未分离'),
                       'start_ms': n['start_ms'], 'text': n['text']} for n in normalized]
            for kind, suffix, text in (
                    ('md', '会议纪要.md', minutes_md.minutes(document, meeting_date=meeting_date, links=links, audio=audio)),
                    ('transcript_md', '逐字稿.md', minutes_md.transcript(document['topic'], spoken, meeting_date=meeting_date,
                                                                         people=document['people'], links=links, all_confirmed=confirmed))):
                path = directory / (sid + '-' + suffix)
                path.write_text(text, encoding='utf-8'); files[kind] = str(path)
            if sid in private['sources']:
                # Private analysis of each person; a separate file, never part of the shared minutes.
                import insights
                try:
                    insights.validate(root, {'sources': {sid: private['sources'][sid]}})
                    text = insights.markdown(root, state, sid, private['sources'][sid], corrections, protected, shown_names,
                                             links, document['topic'], meeting_date)
                except (ValueError, OSError, KeyError) as error:  # e.g. a cited earlier meeting was deleted
                    files['insight_error'] = str(error)
                else:
                    path = directory / (sid + '-深度洞察.md'); path.write_text(text, encoding='utf-8'); files['insight_md'] = str(path)
        if sid in state.get('reader_stale_sources', {}):
            stale = state['reader_stale_sources'][sid]
            names = stale.get('remaining_old_names', stale.get('old_names', []))
            files['reader_error'] = '说话人姓名已修改，正文存在无法安全替换的旧称呼；请更新 analysis 后再导出完整纪要。' + ('还剩：' + '、'.join(names) if names else '旧称呼记录缺失，请先核对正文。')
        output['files'][sid] = files
    write(root / 'exports/latest.json', output)
    return output


def unresolved_audit(root):
    import audit
    saved = read(Path(root) / 'semantic_review.json') if (Path(root) / 'semantic_review.json').exists() else {}
    settled = saved.get('resolutions', {})
    return [p for p in audit.audit(root) if not audit.resolution_valid(p, settled.get(p['id'], {}))]


class SpeakerEditsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.capture = False
        self.blocks = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('id') == 'meeting-speaker-edits':
            if attrs.get('type') != 'application/json':
                raise ValueError('说话人修改数据格式不正确。')
            self.blocks.append(''); self.capture = True
    def handle_data(self, value):
        if self.capture:
            self.blocks[-1] += value
    def handle_endtag(self, tag):
        if tag == 'script': self.capture = False


def import_html(root, html_path, expected):
    """Import only explicit speaker edits. Never execute HTML or import meeting analysis/state."""
    root = Path(root)
    state = read(root / 'state.json'); assert_workspace(root, state); check_revision(state, expected)
    path = Path(html_path)
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError('HTML 文件过大。')
    parser = SpeakerEditsParser(); parser.feed(path.read_text(encoding='utf-8'))
    if len(parser.blocks) != 1:
        raise ValueError('需要唯一的说话人修改数据。')
    data = json.loads(parser.blocks[0])
    if not isinstance(data, dict) or set(data) != {'schema_version', 'workspace_id', 'revision', 'source_id', 'mappings'}:
        raise ValueError('只允许导入说话人映射。')
    if data['schema_version'] != 1 or data['workspace_id'] != workspace_identity(root, state):
        raise ValueError('HTML 不属于此工作区。')
    check_revision(state, data['revision'])
    if data['source_id'] not in {s['source_id'] for s in state['sources']}:
        raise ValueError('HTML 来源不存在。')
    latest = read(root / 'exports/latest.json')
    if latest['revision'] != state['revision'] or not latest['files'].get(data['source_id'], {}).get('html'):
        raise ValueError('此来源没有当前版本可导入的纪要网页。')
    speakers = {s['key']: s for s in state['speakers'] if s['source_id'] == data['source_id'] and not s.get('ignored_by_user')}
    mappings = data['mappings']
    if not isinstance(mappings, list) or len(mappings) > len(speakers):
        raise ValueError('无效的说话人映射。')
    if any(not isinstance(item, dict) or set(item) != {'key', 'name'} for item in mappings):
        raise ValueError('只允许说话人 key 和 name。')
    clean = []; seen = set(); planned_names = {}
    aliases = {a for speaker in speakers.values() for a in (speaker['key'], '说话人 ' + speaker['key'].rsplit('-', 1)[-1])}
    for item in mappings:
        if not isinstance(item, dict) or set(item) != {'key', 'name'}:
            raise ValueError('只允许说话人 key 和 name。')
        key, name = item['key'], item['name']
        if not isinstance(key, str) or key not in speakers or key in seen:
            raise ValueError('说话人不属于此来源或重复。')
        seen.add(key)
        if speakers[key].get('diarization_unassigned') or key.endswith('-UNASSIGNED'):
            raise ValueError('未分离发言不能通过纪要网页认领。')
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError('无效姓名。')
        name = name.strip()
        if not (speakers[key].get('identity_group') or speakers[key].get('voice_profile_id')) and name == minutes_html.shown_name(speakers[key], display_names()):
            continue
        if not minutes_html.valid_speaker_name(name, aliases):
            raise ValueError('请填写真实姓名或明确角色，不能使用未知标签或说话人代号。')
        planned_names[key] = name
        row = {'key': key, 'name': name.strip(), 'entity_type': 'person'}
        candidate = speakers[key].get('identity_group') or speakers[key].get('voice_profile_id')
        profiles = memory.read_store(memory_path(state))['roles']
        profile = next((p for p in profiles if p['id'] == candidate and p.get('active', True) and (p['name'] == name.strip() or minutes_html.shown_name(speakers[key], display_names()) == name.strip())), None)
        if profile: row = {'key': key, 'profile_id': profile['id']}
        clean.append(row)
    final_names = {key: planned_names.get(key, minutes_html.shown_name(speaker, display_names())) for key, speaker in speakers.items()}
    for key, name in planned_names.items():
        if any(other != key and value == name for other, value in final_names.items()):
            raise ValueError('同一来源的两个说话人不能使用同一个名字。')
    if not clean:
        return {'revision': state['revision'], 'imported': 0}
    return save_roles(root, clean, expected, via='html')


def publish(root, source_ids=None, png_overrides=None):
    """Record the checked HTML/PNG (and Markdown copies) of the latest export for archive_meeting.py."""
    import audit
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); latest = read(root / 'exports/latest.json')
        if latest['revision'] != state['revision']:
            raise ValueError('导出不是最新版本；请先运行 export。')
        if not audit.is_checked(root):
            open_ = audit.report(root)['unresolved']
            if not open_:  # every item was settled, but for an earlier export
                raise ValueError('纪要在核对之后又改过：先运行 workspace.py audit 看一遍，没有新问题就用 '
                                 'audit --resolve 传入 {} 确认这一版，再发布。')
            raise ValueError(f'纪要还没核对完：有 {open_} 处内容和原话对不上或没登记处理结果。'
                             '先运行 workspace.py audit，改正文或登记确认后再发布。')
        png_overrides = png_overrides or {}
        targets = source_ids or [sid for sid, f in latest['files'].items() if f.get('html')]
        if not targets:
            raise ValueError('还没有可发布的纪要 HTML（角色确认并写入 analysis 后才会生成）。')
        sections = read(root / 'analysis.json')['sources']
        files = {}
        for sid in targets:
            exported = latest['files'].get(sid, {})
            if not exported.get('html'):
                raise ValueError('来源 ' + sid + ' 还没有纪要 HTML。')
            if (not sections.get(sid, {}).get('reader', {}).get('points')
                    and any(s.get('core_points') for s in state['speakers']
                            if s['source_id'] == sid and not s.get('ignored_by_user'))):
                raise ValueError('来源 ' + sid + ' 仍在使用未绑定逐条证据的观点预览；请补全 reader.points 及 evidence，核对后再发布。')
            png = png_overrides.get(sid) or exported.get('png')
            if not png:
                raise ValueError('来源 ' + sid + ' 没有 PNG：' + exported.get('png_error', '')
                                 + ' 可在 HTML 中导出图片后用 --png ' + sid + '=路径 指定。')
            documents = [('html', exported['html']), ('png', png)] + [(k, exported[k]) for k in ('md', 'transcript_md', 'insight_md') if exported.get(k)]
            files[sid] = {kind: {'path': str(Path(value).resolve()), 'sha256': file_digest(value)} for kind, value in documents}
        publication = read(root / 'publication.json') if (root / 'publication.json').exists() else {}
        if publication.get('export_version') != latest['version']:
            # An old source must not inherit the new global version when only another source is published.
            publication['files'] = {}
        publication.update(export_version=latest['version'], revision=state['revision'], published_at=now())
        publication.setdefault('files', {}).update(files)
        write(root / 'publication.json', publication)
        return publication


def hotwords(root, terms, out, weight=2):
    state = read(Path(root) / 'state.json'); store = memory.read_store(memory_path(state))
    known = {t['canonical'] for t in store['terms'] if t.get('confirmed_by_user') and t.get('active', True)}
    if not terms or set(terms) - known:
        raise ValueError('Select explicit active, user-confirmed canonical terms.')
    if type(weight) is not int or weight not in (1,2,3,4,5):
        raise ValueError('Use a conservative weight from 1 to 5.')
    result = {term: weight for term in sorted(set(terms))}; write(Path(out), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import'); imp.add_argument('--state', required=True); imp.add_argument('--workspace', required=True); imp.add_argument('--audio', action='append')
    for command in ('show', 'export'):
        p = sub.add_parser(command); p.add_argument('--workspace', required=True)
    a = sub.add_parser('analysis'); a.add_argument('--workspace', required=True); a.add_argument('--file', required=True); a.add_argument('--revision', required=True, type=int)
    c = sub.add_parser('candidates'); c.add_argument('--workspace', required=True); c.add_argument('--file', required=True); c.add_argument('--revision', required=True, type=int)
    ct = sub.add_parser('confirm-terms', help='一次应用用户在对话里确认的术语修正'); ct.add_argument('--workspace', required=True); ct.add_argument('--file', required=True); ct.add_argument('--revision', required=True, type=int)
    r = sub.add_parser('roles', help='应用用户在对话里给出的说话人映射'); r.add_argument('--workspace', required=True); r.add_argument('--file', required=True); r.add_argument('--revision', required=True, type=int)
    ih = sub.add_parser('import-html'); ih.add_argument('--workspace', required=True); ih.add_argument('--html', required=True); ih.add_argument('--revision', type=int, required=True)
    pub = sub.add_parser('publish'); pub.add_argument('--workspace', required=True); pub.add_argument('--source', action='append'); pub.add_argument('--png', action='append', help='SID=PATH，浏览器内手动导出的 PNG')
    au = sub.add_parser('audit', help='核对纪要内容和引用原话是否一致'); au.add_argument('--workspace', required=True)
    au.add_argument('--resolve', help='登记处理结果的 JSON：{问题 id: {resolution, note}}'); au.add_argument('--revision', type=int)
    h = sub.add_parser('hotwords'); h.add_argument('--workspace', required=True); h.add_argument('--term', action='append', required=True); h.add_argument('--out', required=True); h.add_argument('--weight', type=int, default=2)
    args = parser.parse_args()
    try:
        if args.command == 'import': result = str(initialize(args.state, args.workspace, pairs(args.audio)))
        elif args.command == 'show': result = public_state(args.workspace)
        elif args.command == 'analysis': result = set_analysis(args.workspace, read(args.file), args.revision)
        elif args.command == 'candidates':
            import term_review
            result = term_review.prepare(args.workspace, read(args.file), args.revision)
        elif args.command == 'confirm-terms':
            import term_review
            result = term_review.confirm_batch(args.workspace, read(args.file), args.revision)
        elif args.command == 'roles': result = save_roles(args.workspace, read(args.file), args.revision, via='chat')
        elif args.command == 'import-html': result = import_html(args.workspace, args.html, args.revision)
        elif args.command == 'publish': result = publish(args.workspace, args.source, {k: str(v) for k, v in pairs(args.png).items()})
        elif args.command == 'hotwords': result = hotwords(args.workspace, args.term, args.out, args.weight)
        elif args.command == 'audit':
            import audit
            if args.resolve:
                if args.revision is None:
                    parser.error('--resolve 需要 --revision。')
                result = audit.resolve(args.workspace, read(args.resolve), args.revision)
            else:
                result = audit.report(args.workspace)
        else: result = export(Path(args.workspace))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
