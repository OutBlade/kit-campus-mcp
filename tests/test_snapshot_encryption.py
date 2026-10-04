import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from kit_campus_mcp.watch import SnapshotStore


class SnapshotEncryptionTests(unittest.TestCase):
    def test_roundtrip_hides_grade_content_and_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            with patch.dict(os.environ, {'KIT_CAMPUS_STATE_KEY': Fernet.generate_key().decode()}):
                store = SnapshotStore(path)
                entries = [{'title': 'PRIVATE_RESULT', 'grade_raw': '1,3'}]
                store.put('grades', entries)
                checked = store.last_checked('grades')
                store.save()
                self.assertNotIn('PRIVATE_RESULT', path.read_text())
                self.assertEqual(json.loads(path.read_text())['format'], 'kit-campus-encrypted-v1')
                reopened = SnapshotStore(path)
                self.assertEqual(reopened.get('grades'), entries)
                self.assertEqual(reopened.last_checked('grades'), checked)
            with patch.dict(os.environ, {'KIT_CAMPUS_STATE_KEY': Fernet.generate_key().decode()}):
                with self.assertRaisesRegex(RuntimeError, 'Cannot decrypt'):
                    SnapshotStore(path)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'requires KIT_CAMPUS_STATE_KEY'):
                    SnapshotStore(path)
