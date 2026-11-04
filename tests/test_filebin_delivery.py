"""Filebin delivery boundaries and retry recovery; all network operations are mocked."""

import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import filebin_delivery as delivery


class FakeFilebin:
    def __init__(self):
        self.bins = {}
        self.uploads = []
        self.downloads = []
        self.locks = []
        self.lose_upload_response_once = False
        self.fail_download_once = False

    def listing(self, bin_id):
        return copy.deepcopy(self.bins.setdefault(bin_id, {
            'bin': {'readonly': False, 'expired_at': '2100-11-11T20:00:00Z'},
            'files': [],
        }))

    def upload(self, bin_id, name, record):
        self.listing(bin_id)
        self.uploads.append((bin_id, name))
        self.bins[bin_id]['files'].append({
            'filename': name, 'bytes': record['size'], 'sha256': record['sha256'],
        })
        if self.lose_upload_response_once:
            self.lose_upload_response_once = False
            raise delivery.DeliveryError('The response was lost after the file was stored.')

    def verify_download(self, bin_id, name, record):
        self.downloads.append((bin_id, name))
        if self.fail_download_once:
            self.fail_download_once = False
            raise delivery.DeliveryError('The complete download did not match.')

    def lock(self, bin_id):
        self.locks.append(bin_id)
        self.bins[bin_id]['bin']['readonly'] = True


class FilebinDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='filebin-delivery-tests-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle_path = self.root / 'bundle.json'
        self.state_path = self.root / 'delivery-state.json'
        self.fake = FakeFilebin()
        self.bundle = self.make_bundle()
        self.write_bundle()

    def make_bundle(self):
        artifacts = {}
        for key in ('long', 'short1', 'short2', 'thumbnail', 'short1-cover', 'short2-cover'):
            video = key in ('long', 'short1', 'short2')
            path = self.root / (key + ('.mp4' if video else '.jpg'))
            path.write_bytes(('Distinct fixture bytes for ' + key).encode())
            verification = {
                'resolution': [1920, 1080] if key == 'long' else [1080, 1920],
            }
            if video:
                verification.update({
                    'full_decode': True, 'duration': 3600 if key == 'long' else 25,
                    'decoded_seconds': 3600 if key == 'long' else 25,
                    'video_codec': 'h264', 'audio_codec': 'aac', 'channels': 2,
                })
            elif key == 'thumbnail':
                verification['resolution'] = [1280, 720]
            else:
                verification['source_offset'] = 22
            artifacts[key] = {
                'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'verification': verification,
            }
        videos = []
        for key in ('long', 'short1', 'short2'):
            video = {
                'key': key, **copy.deepcopy(artifacts[key]),
                'title': 'Quiet test ' + key,
                'description': 'Distinct description ' + key + '\n\nRecording credits:\nFixture recordist, CC BY 3.0; excerpted.',
                'made_for_kids': False, 'contains_synthetic_media': True,
            }
            if key == 'long':
                video.update({'thumbnail': artifacts['thumbnail']['path'],
                              'thumbnail_sha256': artifacts['thumbnail']['sha256']})
            else:
                video.update({'cover_frame': artifacts[key+'-cover']['path'],
                              'cover_frame_sha256': artifacts[key+'-cover']['sha256'],
                              'suggested_cover_offset': 22})
            videos.append(video)
        return {
            'schema_version': 1, 'bundle_id': '2026-11-04-quiet-test', 'ready': True,
            'source_sha256': artifacts['long']['sha256'],
            'artifacts': artifacts, 'videos': videos,
            'intended_channel_handle': '@QuietHoursRelaxingSounds',
            'theme': 'Quiet test', 'recording_credits': 'Fixture recordist, CC BY 3.0; excerpted.',
            'requested_privacy': 'unlisted', 'schedule_enabled': False, 'playlist_id': None,
        }

    def write_bundle(self):
        self.bundle_path.write_text(json.dumps(self.bundle))

    def run_delivery(self, **kwargs):
        with mock.patch.object(delivery, 'FilebinClient', return_value=self.fake), \
                mock.patch('sys.stdout', new=io.StringIO()):
            return delivery.deliver(self.bundle_path, self.state_path, approved=True, **kwargs)

    def test_completed_retry_does_not_upload_or_download_again(self):
        first = self.run_delivery()
        self.assertEqual(first['status'], 'delivered')
        self.assertEqual(len(self.fake.uploads), 7)
        self.assertEqual(len(self.fake.downloads), 7)
        self.assertEqual(len(self.fake.locks), 1)
        second = self.run_delivery()
        self.assertEqual(first['bin'], second['bin'])
        self.assertEqual(len(self.fake.uploads), 7)
        self.assertEqual(len(self.fake.downloads), 7)
        self.assertEqual(len(self.fake.locks), 1)

    def test_lost_upload_receipt_recovers_from_server_without_duplicate_post(self):
        self.fake.lose_upload_response_once = True
        try:
            self.run_delivery()
        except delivery.DeliveryError:
            pass  # Either immediate reconciliation or the explicit retry may recover it.
        state = self.run_delivery()
        self.assertEqual(state['status'], 'delivered')
        self.assertEqual(len(self.fake.uploads), 7)
        self.assertEqual(len(set(self.fake.uploads)), 7)
        self.assertEqual(len(self.fake.downloads), 7)

    def test_failed_download_is_not_marked_verified_or_locked(self):
        self.fake.fail_download_once = True
        with self.assertRaises(delivery.DeliveryError):
            self.run_delivery()
        stored = json.loads(self.state_path.read_text())
        self.assertFalse(any(item.get('download_verified') for item in stored['files'].values()))
        self.assertEqual(self.fake.locks, [])
        self.run_delivery()
        self.assertEqual(len(self.fake.uploads), len(set(self.fake.uploads)))

    def test_missing_or_failed_decode_never_opens_network_client(self):
        for missing in (True, False):
            for item in [self.bundle['videos'][0], self.bundle['artifacts']['long']]:
                item['verification'].pop('full_decode', None)
                if not missing:
                    item['verification']['full_decode'] = False
            self.write_bundle()
            with self.subTest(missing=missing), \
                    mock.patch.object(delivery, 'FilebinClient') as client, \
                    self.assertRaises(delivery.DeliveryError):
                delivery.deliver(self.bundle_path, self.state_path, approved=True)
            client.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_changed_local_media_or_metadata_preserves_prior_state(self):
        self.run_delivery()
        prior = self.state_path.read_bytes()
        path = Path(self.bundle['artifacts']['short1']['path'])
        original = path.read_bytes()
        path.write_bytes(b'Changed after review')
        with self.assertRaises(delivery.DeliveryError):
            self.run_delivery()
        self.assertEqual(self.state_path.read_bytes(), prior)
        path.write_bytes(original)
        self.bundle['videos'][0]['title'] = 'Changed after delivery'
        self.write_bundle()
        with self.assertRaises(delivery.DeliveryError):
            self.run_delivery()
        self.assertEqual(self.state_path.read_bytes(), prior)

    def test_local_only_requires_no_approval_state_or_network(self):
        with mock.patch.object(delivery, 'FilebinClient') as client, \
                mock.patch('sys.stdout', new=io.StringIO()):
            delivery.deliver(self.bundle_path, self.state_path, check_local_only=True)
        client.assert_not_called()
        self.assertFalse(self.state_path.exists())
        with mock.patch.object(delivery, 'FilebinClient') as client, \
                self.assertRaises(delivery.DeliveryError):
            delivery.deliver(self.bundle_path, self.state_path)
        client.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_upload_manifest_includes_only_authorized_media_and_public_copy(self):
        secret = self.root / 'oauth-refresh-token.json'
        secret.write_text('PRIVATE TOKEN MUST NOT LEAVE')
        self.bundle['private_config'] = str(secret)
        self.bundle['artifacts']['not-for-delivery'] = {
            'path': str(secret), 'sha256': hashlib.sha256(secret.read_bytes()).hexdigest(),
        }
        self.write_bundle()
        _, files, _ = delivery.build_manifest(self.bundle_path)
        self.assertEqual(len(files), 7)
        self.assertNotIn(str(secret), [item.get('path') for item in files.values()])
        generated = [item for name, item in files.items() if name.endswith('.txt')]
        self.assertEqual(len(generated), 1)
        text = generated[0].get('text')
        if text is None:
            text = Path(generated[0]['path']).read_text()
        self.assertNotIn('PRIVATE TOKEN MUST NOT LEAVE', text)
        for video in self.bundle['videos']:
            self.assertIn(video['title'], text)
            self.assertIn(video['description'], text)
        self.assertIn('Unlisted', text)
        self.assertIn('22', text)

    def test_relocated_media_and_refreshed_verification_reuse_delivery_identity(self):
        _, _, original_identity = delivery.build_manifest(self.bundle_path)
        self.run_delivery()
        relocated_root = self.root / 'another-cloud-runner'
        relocated_root.mkdir()
        moved = copy.deepcopy(self.bundle)
        for key, artifact in moved['artifacts'].items():
            original = Path(artifact['path'])
            relocated = relocated_root / original.name
            relocated.write_bytes(original.read_bytes())
            artifact['path'] = str(relocated)
            artifact['verification']['verified_at'] = '2026-11-05T09:00:00+02:00'
        for video in moved['videos']:
            key = video['key']
            video['path'] = moved['artifacts'][key]['path']
            video['verification']['verified_at'] = '2026-11-05T09:00:00+02:00'
            if key == 'long':
                video['thumbnail'] = moved['artifacts']['thumbnail']['path']
            else:
                video['cover_frame'] = moved['artifacts'][key+'-cover']['path']
        relocated_bundle = relocated_root / 'bundle.json'
        relocated_bundle.write_text(json.dumps(moved, indent=2))
        _, _, relocated_identity = delivery.build_manifest(relocated_bundle)
        self.assertEqual(original_identity, relocated_identity)
        with mock.patch.object(delivery, 'FilebinClient', return_value=self.fake), \
                mock.patch('sys.stdout', new=io.StringIO()):
            result = delivery.deliver(relocated_bundle, self.state_path, approved=True)
        self.assertEqual(result['status'], 'delivered')
        self.assertEqual(len(self.fake.uploads), 7)
        self.assertEqual(len(self.fake.downloads), 7)

    def test_failed_state_sync_stops_before_network_client_or_upload(self):
        command = ['fixture-state-sync', 'sync']
        failure = subprocess.CalledProcessError(9, command, stderr=b'private-token-do-not-log')
        with mock.patch.object(delivery.subprocess, 'run', side_effect=failure) as sync, \
                mock.patch.object(delivery, 'FilebinClient') as client, \
                self.assertRaises(delivery.DeliveryError) as raised:
            delivery.deliver(self.bundle_path, self.state_path, approved=True,
                             state_sync_command=command)
        client.assert_not_called()
        sync.assert_called_once_with([*command, str(self.state_path.resolve())], check=True, timeout=120)
        self.assertNotIn('private-token-do-not-log', str(raised.exception))
        self.assertTrue(self.state_path.exists())
        stored = json.loads(self.state_path.read_text())
        self.assertTrue(stored['bin'])
        self.assertFalse(any(item.get('uploaded') for item in stored['files'].values()))


if __name__ == '__main__':
    unittest.main()
