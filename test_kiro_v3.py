"""Kiro CLI V2 and V3 native session compatibility and export fidelity tests.

Uses synthetic transcripts under isolated temporary directories only.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'kiro_export_under_test', Path(__file__).with_name('kiro-md.py'))
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)

SID = 'sess_11111111-2222-4333-8444-555555555555'
OLD_ID = '22222222-3333-4444-8555-666666666666'


def event(kind, **props):
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
        for name, path in [('KIRO_V3_SESSIONS_DIR', self.v3root),
                           ('KIRO_CLI_SESSIONS_DIR', self.v2root)]:
            mocked = patch.object(exporter, name, path)
            mocked.start()
            self.addCleanup(mocked.stop)

    def create_v3(self, messages=None, direct=False, sid=SID, **metadata):
        folder = self.v3root / ('' if direct else 'myworkspace') / sid
        folder.mkdir(parents=True)
        meta = {
            'schemaVersion': '1.0.0', 'dataModelVersion': 1,
            'id': sid, 'title': 'Synthetic engineering chat',
            'workspacePaths': ['/synthetic/project'],
            'createdAt': '2026-11-16T12:00:00Z',
            'lastModifiedAt': '2026-11-16T13:00:00Z',
            'modelId': 'synthetic-model', 'agentMode': 'vibe',
            'effortLevel': 'high', 'autopilot': {'enabled': False},
            'memoryConfig': {'mode': 'manual'},
            'specPlanEnabled': True, 'rootPaths': ['/synthetic/project'],
        }
        meta.update(metadata)
        (folder / 'session.json').write_text(json.dumps(meta), encoding='utf8')
        if messages is not None:
            (folder / 'messages.jsonl').write_text(
                ''.join(json.dumps(x) + '\n' for x in messages),
                encoding='utf8')
        return folder

    def test_v3_id_search_and_preview(self):
        self.create_v3(messages=[event('user', content='Question one')])
        self.assertEqual(len(exporter.scan_v3_sessions()), 1)
        for query in (SID, SID[5:], SID[5:14]):
            self.assertEqual(len(exporter.scan_cli_sessions(query)), 1)
        entry = exporter.scan_v3_sessions()[0]
        entry.load_preview()
        self.assertIn('Question one', entry.preview_text)
        self.assertEqual(entry.transcript_state, 'available')

    def test_v3_direct_root_layout(self):
        self.create_v3(direct=True, messages=[event('user', content='direct message')])
        e = exporter.scan_v3_sessions()[0]
        self.assertIn('direct message', exporter.CliSessionParser(e).to_markdown())

    def test_v3_complete_export_metadata_tools_dialogue_usage_and_attachments(self):
        self.create_v3(messages=[
            event('session_start', agentType='synthetic', content='system preface'),
            event('user', content='alpha prompt', documents=[{'path': '/tmp/reference.md'}],
                  images=[{'mimeType': 'image/png', 'name': 'reference.png'}],
                  _meta={'kiro': {'userMessageTag': 'manual'}}),
            event('turn_start', executionId='task1', turnId='t1', boundarySource='user'),
            event('assistant', content='I will run the command.', executionId='task1',
                  reasoningModelId='reason-model', reasoningSignature='encrypted-signature',
                  operationType='text'),
            event('tool_call', toolCallId='cmd1', toolName='execute_pwsh',
                  args={'command': 'echo example', 'run_in_background': False},
                  actionType='powershell', status='requested'),
            event('tool_result', toolCallId='cmd1', content='example output',
                  success=True, durationMs=20),
            event('tool_call', toolCallId='edit1', toolName='str_replace',
                  args={'path': '/tmp/demo.py', 'oldStr': 'old', 'newStr': 'new'},
                  status='requested'),
            event('tool_result', toolCallId='edit1', content='Edited!', success=True),
            event('tool_call', toolCallId='web1', toolName='remote_web_search',
                  args={'query': 'synthetic query'}),
            event('tool_result', toolCallId='web1', content='Search body', success=True),
            event('usage_summary', executionId='task1', elapsedTime=200,
                  status='success', promptTurnSummaries=[{'tokens': 4521}]),
            event('session_metadata', key='model', value={'display': 'Test model'}),
            event('assistant', content='final response', executionId='task1'),
            event('turn_end', executionId='task1', stopReason='end_turn'),
            event('user', content='beta prompt', documents=[], images=[]),
            event('assistant', content='beta answer'),
            event('unknown_future_event', futureValue={'raw': 7}),
        ])
        e = exporter.scan_cli_sessions()[0]
        p = exporter.CliSessionParser(e)
        self.assertEqual(p.session_type, 'Kiro CLI v3')
        self.assertEqual(p.user_turn_count, 2)
        self.assertEqual(p.execution_count, 3)
        self.assertEqual([x['content'] for x in p.data if x['type'] == 'user_message'],
                         ['alpha prompt', 'beta prompt'])
        result = p.to_markdown()
        for needle in ('alpha prompt', 'beta prompt', 'final response',
                       'echo example', 'example output', 'Edited!', 'Search body',
                       'synthetic query', '/tmp/demo.py', 'high',
                       'encrypted-signature', 'reason-model', 'reference.png',
                       'run_in_background', 'elapsedTime', 'promptTurnSummaries',
                       'unknown_future_event', 'futureValue', 'memoryConfig',
                       'specPlanEnabled', 'boundarySource', 'durationMs'):
            self.assertIn(needle, result)
        self.assertLess(result.index('alpha prompt'), result.index('beta prompt'))

    def test_metadata_only_session_is_listed_but_not_misexported(self):
        folder = self.create_v3(messages=None)
        entry = exporter.scan_v3_sessions()[0]
        self.assertEqual(entry.transcript_state, 'missing')
        self.assertFalse(entry.is_empty_helper)
        self.assertIn('NO SAVED CHAT', entry.display_title)
        entry.load_preview()
        self.assertIn('No saved V3 conversation', entry.preview_text)
        with self.assertRaisesRegex(exporter.KiroV3HistoryUnavailable,
                                    'messages.jsonl is missing'):
            exporter.CliSessionParser(entry)
        metadata = json.loads((folder / 'session.json').read_text(encoding='utf8'))
        metadata['schemaVersion'] = '999.0.0'
        (folder / 'session.json').write_text(json.dumps(metadata), encoding='utf8')
        self.assertEqual(exporter.scan_v3_sessions(), [])

    def test_empty_stream_fails_instead_of_fabricating_title(self):
        self.create_v3(messages=[])
        entry = exporter.scan_v3_sessions()[0]
        self.assertEqual(entry.transcript_state, 'no_chat')
        with self.assertRaisesRegex(exporter.KiroV3HistoryUnavailable,
                                    'empty messages.jsonl'):
            exporter.CliSessionParser(entry)

    def test_event_only_stream_fails_closed(self):
        self.create_v3(messages=[event('agent_note', content='internal note'),
                                 event('session_event', category='checkpoint')])
        with self.assertRaisesRegex(exporter.KiroV3HistoryUnavailable,
                                    'no saved user or assistant messages'):
            exporter.CliSessionParser(exporter.scan_v3_sessions()[0])

    def test_invalid_json_refuses_partial_export(self):
        folder = self.create_v3(messages=[event('user', content='good')])
        with (folder / 'messages.jsonl').open('a', encoding='utf8') as output:
            output.write('{invalid\n')
        with self.assertRaisesRegex(exporter.KiroV3HistoryUnavailable,
                                    'refusing partial export'):
            exporter.CliSessionParser(exporter.scan_v3_sessions()[0])

    def test_v2_cli_remains_compatible(self):
        self.create_v3(messages=[event('user', content='V3 prompt'),
                                 event('assistant', content='V3 answer')])
        old = self.v2root / f'{OLD_ID}.json'
        old.write_text(json.dumps({
            'session_id': OLD_ID, 'cwd': '/legacy/project',
            'created_at': '2026-11-01T00:00:00Z',
            'updated_at': '2026-11-01T01:00:00Z', 'title': 'Legacy session',
            'session_state': {'conversation_metadata': {'user_turn_metadatas': []}}
        }), encoding='utf8')
        (self.v2root / f'{OLD_ID}.jsonl').write_text(
            json.dumps({'kind': 'Prompt', 'version': 'v1',
                        'data': {'message_id': 'u1',
                                 'content': [{'kind': 'text', 'data': 'V2 prompt'}]}}) + '\n' +
            json.dumps({'kind': 'AssistantMessage', 'version': 'v1',
                        'data': {'message_id': 'a1',
                                 'content': [{'kind': 'text', 'data': 'V2 answer'}]}}) + '\n',
            encoding='utf8')
        entries = exporter.scan_cli_sessions()
        self.assertEqual(len(entries), 2)
        v2 = next(x for x in entries if x.session_id == OLD_ID)
        v3 = next(x for x in entries if x.session_id == SID)
        self.assertNotIsInstance(v2, exporter.V3SessionEntry)
        self.assertIsInstance(v3, exporter.V3SessionEntry)
        self.assertIn('V2 prompt', exporter.CliSessionParser(v2).to_markdown())
        self.assertIn('V2 answer', exporter.CliSessionParser(v2).to_markdown())
        self.assertIn('V3 prompt', exporter.CliSessionParser(v3).to_markdown())


if __name__ == '__main__':
    unittest.main()
