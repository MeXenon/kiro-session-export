"""Kiro CLI V2 / V3 regression and end-to-end Markdown export tests.

Synthetic fixtures only; never writes into ~/.kiro.
"""
import importlib.util
import json
import tempfile
import zipfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'kiro_export_under_test', Path(__file__).with_name('kiro-md.py'))
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)

SID = 'sess_11111111-2222-4333-8444-555555555555'
OLD_ID = '22222222-3333-4444-8555-666666666666'


def v3_event(kind, **props):
    return {'id': f'event-{kind}-{len(props)}', 'timestamp': '2026-11-16T12:00:00Z',
            'payload': {'type': kind, **props}}


class KiroV3ExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.v3root = self.root / 'v3'
        self.v2root = self.root / 'v2'
        self.v3root.mkdir()
        self.v2root.mkdir()
        self.pv3 = patch.object(exporter, 'KIRO_V3_SESSIONS_DIR', self.v3root)
        self.pv2 = patch.object(exporter, 'KIRO_CLI_SESSIONS_DIR', self.v2root)
        self.pv3.start()
        self.pv2.start()
        self.addCleanup(self.pv3.stop)
        self.addCleanup(self.pv2.stop)

    def create_v3(self, sid=SID, messages=None):
        folder = self.v3root / 'myworkspace' / sid
        folder.mkdir(parents=True)
        meta = {
            'schemaVersion': '1.0.0', 'dataModelVersion': 1,
            'id': sid, 'title': 'Synthetic engineering chat',
            'workspacePaths': ['/synthetic/project'],
            'createdAt': '2026-11-16T12:00:00Z',
            'lastModifiedAt': '2026-11-16T13:00:00Z',
            'modelId': 'synthetic-model', 'agentMode': 'autopilot'}
        (folder / 'session.json').write_text(json.dumps(meta), encoding='utf8')
        if messages is not None:
            (folder / 'messages.jsonl').write_text(
                ''.join(json.dumps(event) + '\n' for event in messages),
                encoding='utf8')
        return folder

    def test_scan_search_bare_and_prefixed_v3_id(self):
        self.create_v3(messages=[v3_event('user', content='Question one')])
        self.assertEqual(len(exporter.scan_v3_sessions()), 1)
        self.assertEqual(len(exporter.scan_cli_sessions(SID)), 1)
        self.assertEqual(len(exporter.scan_cli_sessions(SID[5:])), 1)
        self.assertEqual(len(exporter.scan_cli_sessions(SID[5:14])), 1)
        entry = exporter.scan_v3_sessions()[0]
        entry.load_preview()
        self.assertIn('Question one', entry.preview_text)

    def test_full_v3_export_preserves_order_and_tool_results(self):
        self.create_v3(messages=[
            v3_event('session_start', agentType='synthetic'),
            v3_event('user', content='alpha prompt'),
            v3_event('turn_start', executionId='task1', turnId='t1'),
            v3_event('assistant', content='I will run the command.', executionId='task1'),
            v3_event('tool_call', toolCallId='cmd1', toolName='execute_pwsh',
                     args={'command': 'echo example'}, status='requested'),
            v3_event('tool_result', toolCallId='cmd1', content='example output', success=True),
            v3_event('tool_call', toolCallId='edit1', toolName='str_replace',
                     args={'path': '/tmp/demo.py', 'oldStr': 'old', 'newStr': 'new'}),
            v3_event('tool_result', toolCallId='edit1', content='Edited', success=True),
            v3_event('tool_call', toolCallId='web1', toolName='remote_web_search',
                     args={'query': 'synthetic query'}),
            v3_event('tool_result', toolCallId='web1', content='Search body', success=True),
            v3_event('assistant', content='final response', executionId='task1'),
            v3_event('turn_end', executionId='task1', turnId='t1'),
            v3_event('user', content='beta prompt'),
            v3_event('assistant', content='beta answer'),
        ])
        entry = exporter.scan_cli_sessions()[0]
        parser = exporter.CliSessionParser(entry)
        self.assertEqual(parser.session_type, 'Kiro CLI v3')
        self.assertEqual(parser.user_turn_count, 2)
        self.assertEqual(parser.execution_count, 3)
        self.assertEqual([x['content'] for x in parser.data if x['type'] == 'user_message'],
                         ['alpha prompt', 'beta prompt'])
        md = parser.to_markdown()
        for word in ['alpha prompt', 'beta prompt', 'final response', 'echo example',
                     'example output', '/tmp/demo.py', 'synthetic query', 'Search body']:
            self.assertIn(word, md)
        self.assertLess(md.index('alpha prompt'), md.index('beta prompt'))
        self.assertIn('Kiro CLI v3', md)
        self.assertIn('synthetic-model', md)

    def test_missing_stream_and_wrong_schema(self):
        folder = self.create_v3(messages=None)
        entry = exporter.scan_v3_sessions()[0]
        self.assertTrue(entry.is_empty_helper)
        self.assertEqual(exporter.CliSessionParser(entry).user_turn_count, 0)
        meta_path = folder / 'session.json'
        meta = json.loads(meta_path.read_text())
        meta['schemaVersion'] = '999.0.0'
        meta_path.write_text(json.dumps(meta))
        self.assertEqual(exporter.scan_v3_sessions(), [])

    def test_v2_legacy_and_v3_coexist(self):
        self.create_v3(messages=[v3_event('user', content='V3 prompt'),
                                 v3_event('assistant', content='V3 answer')])
        path = self.v2root / f'{OLD_ID}.json'
        data = {'session_id': OLD_ID, 'cwd': '/legacy/project',
                'created_at': '2026-11-01T00:00:00Z',
                'updated_at': '2026-11-01T01:00:00Z', 'title': 'Legacy session',
                'session_state': {'conversation_metadata': {'user_turn_metadatas': []}}}
        path.write_text(json.dumps(data), encoding='utf8')
        (self.v2root / f'{OLD_ID}.jsonl').write_text(
            json.dumps({'kind': 'Prompt', 'version': 'v1',
                        'data': {'message_id': 'v2u1',
                                 'content': [{'kind': 'text', 'data': 'V2 prompt'}]}}) + '\n' +
            json.dumps({'kind': 'AssistantMessage', 'version': 'v1',
                        'data': {'message_id': 'v2a1',
                                 'content': [{'kind': 'text', 'data': 'V2 answer'}]}}) + '\n',
            encoding='utf8')
        entries = exporter.scan_cli_sessions()
        self.assertEqual(len(entries), 2)
        old = next(e for e in entries if e.session_id == OLD_ID)
        v3 = next(e for e in entries if e.session_id == SID)
        self.assertNotIsInstance(old, exporter.V3SessionEntry)
        self.assertIsInstance(v3, exporter.V3SessionEntry)
        old_md = exporter.CliSessionParser(old).to_markdown()
        v3_md = exporter.CliSessionParser(v3).to_markdown()
        self.assertIn('V2 prompt', old_md)
        self.assertIn('V2 answer', old_md)
        self.assertIn('V3 prompt', v3_md)
        self.assertIn('V3 answer', v3_md)


class KiroV3ArchiveTests(KiroV3ExportTests):
    def test_direct_root_v3_session_directory(self):
        self.create_v3(messages=[v3_event('user', content='direct layout')])
        original = self.v3root / 'myworkspace' / SID
        original.rename(self.v3root / SID)
        entries = exporter.scan_v3_sessions()
        self.assertEqual(len(entries), 1)
        self.assertIn('direct layout', exporter.CliSessionParser(entries[0]).to_markdown())

    def test_zip_root_and_nested_session_roundtrip(self):
        folder = self.create_v3(messages=[
            v3_event('user', content='archive message'),
            v3_event('assistant', content='archive response')])
        for prefix in ('', f'{SID}/'):
            archive = self.root / ('nested.zip' if prefix else 'root.zip')
            with zipfile.ZipFile(archive, 'w') as zf:
                for name in ('session.json', 'messages.jsonl'):
                    zf.write(folder / name, prefix + name)
                # Attempted zip-slip filename should be ignored, never extracted.
                zf.writestr('../../outside.txt', 'must remain in archive')
            with tempfile.TemporaryDirectory() as staging:
                entry = exporter.open_v3_zip_archive(archive, Path(staging))
                md = exporter.CliSessionParser(entry).to_markdown()
                self.assertIn('archive message', md)
                self.assertIn('archive response', md)
                self.assertFalse((Path(staging).parent / 'outside.txt').exists())

    def test_zip_missing_transcript_is_rejected(self):
        folder = self.create_v3(messages=[v3_event('user', content='irrelevant')])
        archive = self.root / 'missing.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.write(folder / 'session.json', 'session.json')
        with tempfile.TemporaryDirectory() as staging:
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                exporter.open_v3_zip_archive(archive, Path(staging))



if __name__ == '__main__':
    unittest.main()
