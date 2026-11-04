import io
import json
import tempfile
import unittest

from submission_api import App, digest


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(self.temp.name)

    def request(self, path, body, token=None, method='POST'):
        data = json.dumps(body).encode()
        environ = dict(PATH_INFO=path, REQUEST_METHOD=method, CONTENT_TYPE='application/json',
                       CONTENT_LENGTH=str(len(data)), **{'wsgi.input': io.BytesIO(data)})
        if token:
            environ['HTTP_AUTHORIZATION'] = 'Bearer ' + token
        response = []
        payload = b''.join(self.app(environ, lambda status, headers: response.append(status)))
        return int(response[0].split()[0]), json.loads(payload)

    def register(self):
        status, result = self.request('/v1/contributors', dict(consent=True))
        self.assertEqual(status, 201)
        self.assertFalse(result['eligible_for_promotion'])
        return result['token']

    def proposal(self):
        return dict(episode=[2316,4,10], duration=1323,
                    anchors=[['chromaprint2-fixed-32-v1',0,'a'*64],
                             ['chromaprint2-fixed-32-v1',16,'b'*64]],
                    marker='intro', start=94, end=126, origin='manual')

    def test_consent_and_credential_hash_only(self):
        self.assertEqual(self.request('/v1/contributors', dict(consent=False))[0], 400)
        token = self.register()
        with self.app.db() as db:
            row = db.execute('SELECT * FROM credentials').fetchone()
        self.assertEqual(row['hash'], digest(token))
        self.assertNotIn(token, tuple(row))

    def test_pending_idempotent_and_numeric_normalization(self):
        token = self.register()
        first = self.request('/v1/proposals', self.proposal(), token)
        other = self.proposal()
        other['duration'] = 1323.0
        other['anchors'].reverse()
        self.assertEqual(first, self.request('/v1/proposals', other, token))
        self.assertEqual(first[0], 202)
        self.assertFalse(first[1]['published'])
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)

    def test_reject_private_urls_roles_and_bad_ranges(self):
        token = self.register()
        for extra in ('url', 'device', 'trusted', 'reviewer'):
            body = self.proposal()
            body[extra] = 'unwanted'
            self.assertEqual(self.request('/v1/proposals', body, token)[0], 400)
        for value in (True, -1, 99999, float('nan')):
            body = self.proposal()
            body['end'] = value
            self.assertEqual(self.request('/v1/proposals', body, token)[0], 400)

    def test_auth_and_revocation(self):
        self.assertEqual(self.request('/v1/proposals', self.proposal())[0], 401)
        token = self.register()
        with self.app.db() as db:
            db.execute('UPDATE credentials SET revoked=1')
        self.assertEqual(self.request('/v1/proposals', self.proposal(), token)[0], 401)

    def test_missing_audio_evidence_stays_unverified_and_rating_is_not_counted(self):
        body = dict(self.proposal(), anchors=[], assessment='bad', reference=dict(marker='b'*64, revision=1))
        status, result = self.request('/v1/proposals', body, self.register())
        self.assertEqual(status, 202)
        self.assertEqual(result['state'], 'pending_cut_verification')
        self.assertFalse(result['published'])
        self.assertFalse(result['counted_vote'])
        with self.app.votes.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 0)

    def test_votes_known_targets_only_latest_vote_wins(self):
        token = self.register()
        self.assertEqual(self.request('/v1/votes', dict(target='a'*64, good=True), token)[0], 404)
        target = self.app.registry.admit(self.proposal(),confirmed_cut=True,operator='test administrator')
        for value in (True, True, False):
            self.assertEqual(self.request('/v1/votes', dict(target=target, good=value), token)[0], 200)
        with self.app.votes.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT good FROM votes').fetchone()[0], 0)

    def test_quota_and_body_size(self):
        token = self.register()
        actor = self.app.votes.anonymous(digest(token))
        for _ in range(60):
            self.app.limit(actor)
        self.assertEqual(self.request('/v1/proposals', self.proposal(), token)[0], 429)
        self.assertEqual(self.request('/v1/contributors', dict(consent=True, junk='x'*9000))[0], 413)


if __name__ == '__main__':
    unittest.main()
