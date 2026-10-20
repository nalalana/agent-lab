import hashlib
import secrets
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from apps.user.models import CompanyInvitation


class Command(BaseCommand):
    help = '内部生成公司注册邀请码；明文仅在生成时显示，请安全发放'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=1)
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--uses', type=int, default=1)

    def handle(self, *args, **options):
        if not 1 <= options['count'] <= 1000 or not 1 <= options['days'] <= 365 or not 1 <= options['uses'] <= 1000:
            raise CommandError('count/uses 范围 1–1000，days 范围 1–365')
        for _ in range(options['count']):
            code = secrets.token_urlsafe(24)
            CompanyInvitation.objects.create(code_hash=hashlib.sha256(code.encode()).hexdigest(),
                expires_at=timezone.now() + timedelta(days=options['days']), remaining_uses=options['uses'])
            self.stdout.write(code)
