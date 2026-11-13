"""Tests for LINE reference-note injection into the existing CaptionClient."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import sys
import types

# In lightweight test environments OpenAI is mocked, not called.
try:
    import openai
except ImportError:
    stub = types.ModuleType('openai')
    stub.OpenAI = object
    sys.modules['openai'] = stub

from caption_client import CaptionClient

FOLDER = 'fixture_bisyu_inbox'

class CaptionIntegrationTests(unittest.TestCase):
    def test_bisyu_notes_and_other_account_isolation(self):
        config = SimpleNamespace(OPENAI_API_KEY='fake', OPENAI_MODEL='example-model',
                                 CAPTION_PROMPT='default instruction')
        fake_response = SimpleNamespace(output_text='generated caption ')
        fake_client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: fake_response))
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return fake_response
        fake_client.responses.create = create
        notes = {'drive1': '本日のお知らせ', 'drive2': '本日のお知らせ', 'drive3': '別のお知らせ'}
        with TemporaryDirectory() as tmp:
            image = Path(tmp) / 'image.jpg'
            image.write_bytes(b'test image')
            photos = [{'drive_file_id': file_id, 'local_path': str(image), 'mime_type': 'image/jpeg'}
                      for file_id in notes]
            bisyu = SimpleNamespace(drive_inbox_folder_id=FOLDER, caption_prompt='美趣の指示')
            other = SimpleNamespace(drive_inbox_folder_id='some-other-folder', caption_prompt='他社の指示')
            with patch('caption_client.OpenAI', return_value=fake_client), \
                 patch('line_drive_common.get_setting', side_effect=lambda name: FOLDER if name == 'BISYU_DRIVE_INBOX_FOLDER_ID' else ''), \
                 patch('line_drive_common.get_caption_for_drive_file', side_effect=notes.get):
                self.assertEqual('generated caption', CaptionClient(config, bisyu).generate(photos, '追加指示'))
                prompt = calls[-1]['input'][0]['content'][0]['text']
                self.assertEqual(1, prompt.count('本日のお知らせ'))
                self.assertEqual(1, prompt.count('別のお知らせ'))
                self.assertIn('美趣の指示', prompt)
                self.assertIn('追加指示', prompt)
                self.assertEqual('generated caption', CaptionClient(config, other).generate(photos))
                self.assertEqual('他社の指示', calls[-1]['input'][0]['content'][0]['text'])

if __name__ == '__main__':
    unittest.main()
