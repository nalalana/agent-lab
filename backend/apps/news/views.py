import json
import io
import logging
import uuid

from PIL import Image, UnidentifiedImageError
from apps.moderation.models import ImageModerationAsset
from apps.moderation.services import ImageModerationService
from common.utils.Tengcentcos_utils import put_object_with_key, get_moderated_image_presigned_url

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from apps.user.models import User
from apps.user.permissions import is_platform_administrator
from common.utils.Login_utils import login_checker
from .models import NewsHotspot


def can_publish(user_id):
    return is_platform_administrator(user_id)


@require_GET
@login_checker
def access(request):
    response = JsonResponse({'can_publish': can_publish(request.user_id)})
    response['Cache-Control'] = 'no-store'
    return response


def serialize(news, detail=False):
    data = {
        'id': news.id, 'title': news.title, 'summary': news.summary,
        'published_at': news.published_at.isoformat(),
    }
    if detail:
        data['content'] = news.content
        data['body'] = [
            {**block, 'url': get_moderated_image_presigned_url(block['key'])}
            if block['type'] == 'image' else block for block in news.body
        ]
    return data


def validate_news(request, existing=None):
    """Validate a complete article; keep approved images already on this article."""
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'message': '请求格式错误'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'message': '请求格式错误'}, status=400)
    values = {}
    for field, label, limit in [('title', '标题', 120), ('summary', '摘要', 300), ('content', '正文', 50000)]:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            return JsonResponse({'message': f'{label}不能为空且不能超过 {limit} 个字符'}, status=400)
        values[field] = value.strip()
    body = data.get('body', [])
    if not isinstance(body, list) or len(body) > 2000:
        return JsonResponse({'message': '正文结构错误或段落过多'}, status=400)
    clean_body = []
    image_count = 0
    text_length = 0
    owner = User.objects.get(pk=request.user_id)
    existing_keys = {
        block['key'] for block in (existing.body if existing else [])
        if block.get('type') == 'image' and isinstance(block.get('key'), str)
    }
    for block in body:
        if not isinstance(block, dict):
            return JsonResponse({'message': '正文结构错误'}, status=400)
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            text_length += len(block['text'])
            if text_length > 50000:
                return JsonResponse({'message': '正文不能超过 50000 字'}, status=400)
            clean_body.append({'type': 'text', 'text': block['text']})
        elif block.get('type') == 'image' and isinstance(block.get('key'), str):
            image_count += 1
            key = block['key']
            owned_upload = ImageModerationAsset.objects.filter(
                storage_key=key, source='news.image', owner_type='user', owner_id=owner.uid,
            ).exists()
            if (image_count > 20 or (key not in existing_keys and not owned_upload)
                    or not ImageModerationService.is_publicly_viewable(key)):
                return JsonResponse({'message': '图片无效、未通过审核或超过 20 张'}, status=400)
            clean_body.append({'type': 'image', 'key': key})
        else:
            return JsonResponse({'message': '正文结构错误'}, status=400)
    if clean_body:
        values['content'] = ''.join(b['text'] for b in clean_body if b['type'] == 'text').strip()
        if not values['content']:
            return JsonResponse({'message': '请填写正文文字'}, status=400)
    return {**values, 'body': clean_body}


@login_checker
def publish(request):
    if not can_publish(request.user_id):
        return JsonResponse({'message': '仅平台管理员可以发布新闻热点'}, status=403)
    values = validate_news(request)
    if isinstance(values, JsonResponse):
        return values
    news = NewsHotspot.objects.create(author_id=request.user_id, **values)
    return JsonResponse(serialize(news, detail=True), status=201)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def collection(request):
    if request.method == 'POST':
        return publish(request)
    try:
        before = int(request.GET.get('before', '0'))
        if before < 0:
            raise ValueError
    except ValueError:
        return JsonResponse({'message': '分页参数错误'}, status=400)
    queryset = NewsHotspot.objects.all()
    if before:
        queryset = queryset.filter(id__lt=before)
    # IDs follow publication order; fetching one extra row avoids a count query.
    rows = list(queryset.order_by('-id')[:21])
    return JsonResponse({'items': [serialize(row) for row in rows[:20]],
                         'next_cursor': rows[19].id if len(rows) > 20 else None})


@login_checker
def manage(request, news_id):
    if not can_publish(request.user_id):
        return JsonResponse({'message': '仅平台管理员可以管理新闻热点'}, status=403)
    with transaction.atomic():
        news = NewsHotspot.objects.select_for_update().filter(pk=news_id).first()
        if news is None:
            return JsonResponse({'message': '新闻不存在或已删除'}, status=404)
        if request.method == 'DELETE':
            news.delete()
            return JsonResponse({'message': '新闻已删除'})
        values = validate_news(request, existing=news)
        if isinstance(values, JsonResponse):
            return values
        for field, value in values.items():
            setattr(news, field, value)
        # Editing must not change the original author or publication order.
        news.save(update_fields=list(values))
    return JsonResponse(serialize(news, detail=True))


@csrf_exempt
@require_http_methods(['GET', 'PUT', 'DELETE'])
def detail(request, news_id):
    if request.method != 'GET':
        return manage(request, news_id)
    news = NewsHotspot.objects.filter(pk=news_id).first()
    if news is None:
        return JsonResponse({'message': '新闻不存在'}, status=404)
    return JsonResponse(serialize(news, detail=True))


@csrf_exempt
@require_http_methods(['POST'])
@login_checker
def upload_image(request):
    if not can_publish(request.user_id):
        return JsonResponse({'message': '仅平台管理员可以上传新闻图片'}, status=403)
    file = request.FILES.get('file')
    if not file or file.size > 10 * 1024 * 1024:
        return JsonResponse({'message': '请选择不超过 10MB 的图片'}, status=400)
    raw = file.read()
    try:
        with Image.open(io.BytesIO(raw)) as image:
            fmt = image.format
            if fmt not in {'JPEG', 'PNG', 'GIF', 'WEBP'} or image.width * image.height > 25000000:
                raise ValueError
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return JsonResponse({'message': '仅支持有效的 JPG、PNG、GIF、WEBP 图片，最多 2500 万像素'}, status=400)
    mime = Image.MIME[fmt]
    key = f'news/{uuid.uuid4().hex}.{fmt.lower()}'
    try:
        put_object_with_key(raw, key, mime)
        user = User.objects.get(pk=request.user_id)
        ImageModerationService.register_uploaded_image(
            key, source='news.image', source_field='body', owner_type='user', owner_id=user.uid,
            mime_type=mime, size=len(raw),
        )
        url = get_moderated_image_presigned_url(key)
        if not url:
            return JsonResponse({'message': '图片未通过审核或暂不可用，请稍后重试'}, status=422)
        return JsonResponse({'key': key, 'url': url}, status=201)
    except Exception:
        logging.getLogger(__name__).exception('News image upload failed')
        return JsonResponse({'message': '图片上传失败，请稍后重试'}, status=503)
