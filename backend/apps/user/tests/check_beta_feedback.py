"""Run: python backend/apps/user/tests/check_beta_feedback.py (isolated SQLite, mocked COS)."""
import os
import sys
import io
import json
import hashlib
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.test')
from django.conf import settings
settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
import django
django.setup()
from django.db import connection
from django.test import RequestFactory
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from apps.user.models import User, CompanyInvitation, CustomerFeedback
from apps.user.feedback import collection, image, validate_images, MAX_IMAGE_BYTES
from apps.company.views import company_create
from apps.company.serializers import CompanyCreateSerializer
assert CompanyCreateSerializer().fields["invitation_code"].required
from common.utils.Login_utils import sign_token

with connection.schema_editor() as editor:
    for model in (User, CompanyInvitation, CustomerFeedback):
        editor.create_model(model)
user = User.objects.create(uid='tester', username='tester', created_at='', updated_at='')
factory = RequestFactory()
headers = {'HTTP_AUTHORIZATION': 'Bearer ' + sign_token({'user_id': user.pk})}
assert collection(factory.get('/feedback', **headers)).status_code == 403
assert image(factory.get('/feedback/1/images/0', **headers), 1, 0).status_code == 403
assert collection(factory.get('/feedback')).status_code == 401
raw = io.BytesIO()
Image.new('RGB', (10,10)).save(raw, format='PNG')
def upload(): return SimpleUploadedFile('image.png', raw.getvalue(), content_type='image/png')
assert len(validate_images([upload()])) == 1
for files in ([upload()] * 6, [SimpleUploadedFile('big.png', b'x' * (MAX_IMAGE_BYTES + 1))], [SimpleUploadedFile('fake.png', b'fake')]):
    try: validate_images(files)
    except ValueError: pass
    else: raise AssertionError('invalid images accepted')
with patch('apps.user.feedback.put_object_with_key') as put:
    response = collection(factory.post('/feedback', {'category': '功能问题', 'rating': '5', 'description': '测试反馈', 'images': [upload()]}, **headers))
    assert response.status_code == 201, response.content
    assert put.call_args.kwargs['private'] is True
assert collection(factory.post('/feedback', {'category':'其他', 'rating':'6', 'description':'test'}, **headers)).status_code == 400
user.role='admin'; user.save()
assert collection(factory.get('/feedback', **headers)).status_code == 200
with patch('apps.user.feedback.get_object_bytes', return_value=b'image'):
    assert image(factory.get('/feedback/1/images/0', **headers), 1, 0).status_code == 200
user.role='user'; user.save()
assert collection(factory.get('/feedback', **headers)).status_code == 403
invite = CompanyInvitation.objects.create(code_hash=hashlib.sha256(b'secret').hexdigest(), expires_at=timezone.now()+timedelta(days=1))
# Mock unrelated enterprise provisioning; exercise actual API invite validation and transaction.
class Serializer:
    def __init__(self, data): self.validated_data=data; self.errors={}
    def is_valid(self): return True
class Company: cid='company-id'
def create(code):
    return company_create(factory.post('/company/create', json.dumps({'company_name':'Test', 'invitation_code':code}), content_type='application/json', **headers))
with patch('apps.company.views.CompanyCreateSerializer', Serializer), patch('apps.company.views.get_user_from_request', return_value=user), patch('apps.company.views.CompanyService.create_company') as create_service:
    assert create('wrong').status_code == 400
    create_service.assert_not_called()
    create_service.return_value=(False, 'failed')
    create('secret'); invite.refresh_from_db(); assert invite.remaining_uses == 1
    create_service.return_value=(True, Company())
    assert create('secret').status_code == 200
    invite.refresh_from_db(); assert invite.remaining_uses == 0
    assert create('secret').status_code == 400
    invite.remaining_uses=1; invite.expires_at=timezone.now()-timedelta(seconds=1); invite.save()
    assert create('secret').status_code == 400
print('PASS: invite validation/expiry/consumption, feedback access/submission, private images and upload limits')
