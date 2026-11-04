"""Receipt validation and monotonic retry recovery; no network calls."""
import copy
import unittest

from cloud_ledger import LedgerError
import filebin_cloud_state as state


class FilebinStateTests(unittest.TestCase):
    def receipt(self):
        name = '2026-11-04-test-upload-details.txt'
        item = {'size': 20, 'sha256': 'a' * 64}
        return {'schema_version': 1, 'provider': 'filebin.net', 'bin': 'b' * 32,
                'bundle_id': '2026-11-04-test', 'identity': 'c' * 64,
                'expected_files': {name: item}, 'files': {}}

    def test_private_fields_and_foreign_urls_are_rejected(self):
        valid = self.receipt()
        for field in ('token', 'path', 'text', 'source_url'):
            bad = copy.deepcopy(valid)
            bad[field] = 'must never be copied'
            with self.subTest(field=field), self.assertRaises(LedgerError):
                state.validate(bad)
        name, item = next(iter(valid['expected_files'].items()))
        valid['files'][name] = {**item, 'url': 'https://example.invalid/' + name}
        with self.assertRaises(LedgerError):
            state.validate(valid)

    def test_completed_receipts_are_not_downgraded_on_retry(self):
        local = self.receipt()
        remote = copy.deepcopy(local)
        name, item = next(iter(remote['expected_files'].items()))
        base = 'https://filebin.net/' + remote['bin']
        remote['files'][name] = {**item, 'url': base + '/' + name,
                                 'uploaded': True, 'download_verified': True,
                                 'download_verification_method': 'one complete HTTP GET, SHA-256 and byte count'}
        remote.update({'status': 'delivered', 'readonly': True, 'folder_url': base,
                       'expires_at': '2026-11-11T18:00:00Z'})
        self.assertEqual(state.merge(remote, local), remote)
        local['bin'] = 'd' * 32
        with self.assertRaises(LedgerError):
            state.merge(remote, local)

    def test_delivery_cannot_be_claimed_without_complete_checks(self):
        data = self.receipt()
        data.update({'status': 'delivered', 'readonly': True})
        with self.assertRaises(LedgerError):
            state.validate(data)


if __name__ == '__main__':
    unittest.main()
