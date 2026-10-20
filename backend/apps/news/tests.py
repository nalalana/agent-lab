import json
import io
from unittest.mock import patch
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from apps.moderation.models import ImageModerationAsset

from django.test import TestCase, override_settings

from apps.user.models import User
from common.utils.Login_utils import sign_token
from .models import NewsHotspot


@override_settings(ROOT_URLCONF='config.urls')
class NewsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(uid='news-admin', username='Admin', role='admin')
        self.reader = User.objects.create(uid='news-reader', username='Reader', role='user')
        self.payload = {'title': '新闻标题', 'summary': '新闻摘要', 'content': '第一段\n第二段'}

    def headers(self, user):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + sign_token({'user_id': user.id})}

    def publish(self, payload, user=None):
        return self.client.post('/api/news', json.dumps(payload), content_type='application/json',
                                **(self.headers(user) if user else {}))

    def test_publication_permissions_validation_and_public_reads(self):
        self.assertEqual(self.publish(self.payload).status_code, 401)
        self.assertEqual(self.publish(self.payload, self.reader).status_code, 403)
        self.assertFalse(self.client.get('/api/news/access', **self.headers(self.reader)).json()['can_publish'])
        self.assertTrue(self.client.get('/api/news/access', **self.headers(self.admin)).json()['can_publish'])
        for payload in [[], {**self.payload, 'title': ' '}, {**self.payload, 'title': 'x' * 121},
                        {**self.payload, 'summary': 1}, {**self.payload, 'content': 'x' * 50001}]:
            self.assertEqual(self.publish(payload, self.admin).status_code, 400)
        self.assertEqual(NewsHotspot.objects.count(), 0)
        response = self.publish({**self.payload, 'author_id': self.reader.id}, self.admin)
        self.assertEqual(response.status_code, 201)
        news = NewsHotspot.objects.get()
        self.assertEqual(news.author_id, self.admin.id)
        self.assertEqual(self.client.get(f'/api/news/{news.id}').json()['content'], self.payload['content'])
        self.assertEqual(self.client.get('/api/news/99999').status_code, 404)
        self.assertNotIn('content', self.client.get('/api/news').json()['items'][0])
        self.admin.role = 'user'
        self.admin.save()
        self.assertEqual(self.publish(self.payload, self.admin).status_code, 403)
        self.admin.role = 'root'
        self.admin.save()
        self.assertEqual(self.publish(self.payload, self.admin).status_code, 201)

    def test_cursor_pagination(self):
        NewsHotspot.objects.bulk_create([NewsHotspot(**self.payload) for _ in range(25)])
        first = self.client.get('/api/news').json()
        self.assertEqual(len(first['items']), 20)
        self.assertGreater(first['items'][0]['id'], first['items'][-1]['id'])
        newest = NewsHotspot.objects.create(**self.payload)
        second = self.client.get('/api/news', {'before': first['next_cursor']}).json()
        self.assertEqual(len(second['items']), 5)
        self.assertIsNone(second['next_cursor'])
        ids = [row['id'] for row in first['items'] + second['items']]
        self.assertEqual(len(set(ids)), 25)
        self.assertNotIn(newest.id, ids)
        self.assertEqual(self.client.get('/api/news?before=bad').status_code, 400)

    @patch('apps.news.views.get_moderated_image_presigned_url', return_value='https://example.com/news.png')
    @patch('apps.news.views.ImageModerationService.is_publicly_viewable', return_value=True)
    def test_image_body_requires_owned_news_upload_and_keeps_order(self, *_):
        ImageModerationAsset.objects.create(storage_key='news/test.png', source='news.image',
                                            owner_type='user', owner_id=self.admin.uid)
        body = [{'type': 'text', 'text': '前文\n'}, {'type': 'image', 'key': 'news/test.png', 'url': 'javascript:evil'},
                {'type': 'text', 'text': '\n后文'}]
        response = self.publish({**self.payload, 'body': body}, self.admin)
        self.assertEqual(response.status_code, 201)
        saved = NewsHotspot.objects.get()
        self.assertNotIn('url', saved.body[1])
        self.assertEqual(saved.content, '前文\n\n后文')
        self.assertEqual(response.json()['body'][1]['url'], 'https://example.com/news.png')
        for invalid in [[{'type': 'image', 'key': 'private/key'}], [{'type': 'html', 'text': '<script>'}],
                        [{'type': 'text', 'text': 'x' * 50001}], [body[1]] * 21]:
            self.assertEqual(self.publish({**self.payload, 'body': invalid}, self.admin).status_code, 400)

    @patch('apps.news.views.get_moderated_image_presigned_url', return_value='https://example.com/news.png')
    @patch('apps.news.views.ImageModerationService.register_uploaded_image')
    @patch('apps.news.views.put_object_with_key')
    def test_upload_checks_role_and_image_bytes(self, store, register, url):
        def upload(raw, user):
            return self.client.post('/api/news/images', {'file': SimpleUploadedFile('image.png', raw, 'image/png')}, **self.headers(user))
        self.assertEqual(upload(b'fake', self.reader).status_code, 403)
        self.assertEqual(upload(b'<svg onload="alert(1)"></svg>', self.admin).status_code, 400)
        store.assert_not_called()
        data = io.BytesIO()
        Image.new('RGB', (2, 2)).save(data, format='PNG')
        self.assertEqual(upload(data.getvalue(), self.admin).status_code, 201)
        self.assertTrue(store.call_args.args[1].startswith('news/'))
        self.assertEqual(register.call_args.kwargs['owner_id'], self.admin.uid)
        url.return_value = None
        self.assertEqual(upload(data.getvalue(), self.admin).status_code, 422)

    def edit(self, news_id, payload, user=None):
        return self.client.put(f'/api/news/{news_id}', json.dumps(payload), content_type='application/json',
                               **(self.headers(user) if user else {}))

    def test_edit_permissions_preserve_author_and_publication_date(self):
        news = NewsHotspot.objects.create(author=self.admin, **self.payload)
        published_at = news.published_at
        changes = {**self.payload, 'title': '修改后的标题', 'content': '修改后的正文',
                   'author_id': self.reader.id, 'published_at': '2000-01-01T00:00:00Z'}
        self.assertEqual(self.edit(news.id, changes).status_code, 401)
        self.assertEqual(self.edit(news.id, changes, self.reader).status_code, 403)
        other_admin = User.objects.create(uid='news-other-admin', username='Other', role='admin')
        self.assertEqual(self.edit(news.id, changes, other_admin).status_code, 200)
        news.refresh_from_db()
        self.assertEqual(news.title, changes['title'])
        self.assertEqual(news.content, changes['content'])
        self.assertEqual(news.author_id, self.admin.id)
        self.assertEqual(news.published_at, published_at)
        self.assertEqual(self.client.get(f'/api/news/{news.id}').json()['title'], changes['title'])
        self.assertEqual(self.client.get('/api/news').json()['items'][0]['title'], changes['title'])
        other_admin.role = 'user'
        other_admin.save()
        self.assertEqual(self.edit(news.id, self.payload, other_admin).status_code, 403)

    def test_invalid_edit_is_atomic_and_missing_articles_are_not_recreated(self):
        news = NewsHotspot.objects.create(author=self.admin, **self.payload)
        for payload in [[], {**self.payload, 'title': ''}, {**self.payload, 'summary': 'x' * 301},
                        {**self.payload, 'body': 'bad'}, {**self.payload, 'body': [{}]}]:
            self.assertEqual(self.edit(news.id, payload, self.admin).status_code, 400)
            news.refresh_from_db()
            self.assertEqual(news.title, self.payload['title'])
            self.assertEqual(news.content, self.payload['content'])
        self.assertEqual(self.client.put(f'/api/news/{news.id}', b'{bad', content_type='application/json',
                                         **self.headers(self.admin)).status_code, 400)
        self.assertEqual(self.edit(999999, self.payload, self.admin).status_code, 404)
        self.assertEqual(self.client.patch(f'/api/news/{news.id}', '{}', content_type='application/json',
                                           **self.headers(self.admin)).status_code, 405)
        self.assertEqual(NewsHotspot.objects.count(), 1)

    @patch('apps.news.views.get_moderated_image_presigned_url', return_value='https://example.com/news.png')
    @patch('apps.news.views.ImageModerationService.is_publicly_viewable', return_value=True)
    def test_edit_retains_existing_images_but_rejects_unrelated_uploads(self, viewable, _url):
        original_key = 'news/original.png'
        ImageModerationAsset.objects.create(storage_key=original_key, source='news.image',
                                            owner_type='user', owner_id=self.admin.uid)
        ImageModerationAsset.objects.create(storage_key='news/unrelated.png', source='news.image',
                                            owner_type='user', owner_id=self.admin.uid)
        body = [{'type': 'text', 'text': '前文'}, {'type': 'image', 'key': original_key},
                {'type': 'text', 'text': '后文'}]
        news = NewsHotspot.objects.create(author=self.admin, body=body, **self.payload)
        editor = User.objects.create(uid='news-root', username='Root', role='root')
        response = self.edit(news.id, {**self.payload, 'body': body}, editor)
        self.assertEqual(response.status_code, 200)
        news.refresh_from_db()
        self.assertEqual(news.body, body)
        self.assertEqual(news.content, '前文后文')
        ImageModerationAsset.objects.create(storage_key='news/new.png', source='news.image',
                                            owner_type='user', owner_id=editor.uid)
        with_new = body + [{'type': 'image', 'key': 'news/new.png', 'url': 'javascript:evil'}]
        self.assertEqual(self.edit(news.id, {**self.payload, 'body': with_new}, editor).status_code, 200)
        news.refresh_from_db()
        self.assertNotIn('url', news.body[-1])
        for invalid in [
            body + [{'type': 'image', 'key': 'news/unrelated.png'}],
            body + [{'type': 'image', 'key': 'private/key'}],
            [body[0]] + [body[1]] * 21,
        ]:
            self.assertEqual(self.edit(news.id, {**self.payload, 'body': invalid}, editor).status_code, 400)
        viewable.return_value = False
        self.assertEqual(self.edit(news.id, {**self.payload, 'body': body}, editor).status_code, 400)

    def test_delete_requires_current_admin_role_and_removes_public_article(self):
        news = NewsHotspot.objects.create(author=self.admin, **self.payload)
        url = f'/api/news/{news.id}'
        self.assertEqual(self.client.delete(url).status_code, 401)
        self.assertEqual(self.client.delete(url, **self.headers(self.reader)).status_code, 403)
        self.admin.role = 'user'
        self.admin.save()
        self.assertEqual(self.client.delete(url, **self.headers(self.admin)).status_code, 403)
        self.assertTrue(NewsHotspot.objects.filter(pk=news.id).exists())
        self.admin.role = 'root'
        self.admin.save()
        self.assertEqual(self.client.delete(url, **self.headers(self.admin)).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get('/api/news').json()['items'], [])
        self.assertEqual(self.client.delete(url, **self.headers(self.admin)).status_code, 404)
        self.assertEqual(self.edit(news.id, self.payload, self.admin).status_code, 404)
