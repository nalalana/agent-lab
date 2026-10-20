import io
import logging
import uuid
from PIL import Image, UnidentifiedImageError
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET
from common.utils.Login_utils import login_checker
from common.utils.Tengcentcos_utils import put_object_with_key, get_object_bytes, delete_object
from apps.user.permissions import is_platform_administrator
from apps.user.models import CustomerFeedback

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024
CATEGORIES = ('功能问题', '使用建议', '账号与企业', '其他')
logger = logging.getLogger('touming')


def validate_images(files):
    if len(files) > MAX_IMAGES:
        raise ValueError('最多上传 5 张图片')
    images = []
    for file in files:
        if file.size > MAX_IMAGE_BYTES:
            raise ValueError('每张图片不能超过 5 MB')
        raw = file.read(MAX_IMAGE_BYTES + 1)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError('每张图片不能超过 5 MB')
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 20000000:
                    raise ValueError('仅支持不超过 2000 万像素的 JPG、PNG、WebP 图片')
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                output = io.BytesIO()
                image.convert('RGB').save(output, format='JPEG', quality=85)
            if output.tell() > MAX_IMAGE_BYTES:
                raise ValueError('处理后的图片超过 5 MB，请压缩后上传')
            images.append(output.getvalue())
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('图片文件无效') from exc
    return images


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@login_checker
def collection(request):
    if request.method == 'GET':
        if not is_platform_administrator(request.user_id):
            return JsonResponse({'message': '仅平台管理员可以查看反馈'}, status=403)
        try:
            offset = max(0, int(request.GET.get('offset', 0)))
        except ValueError:
            return JsonResponse({'message': '分页参数无效'}, status=400)
        rows = CustomerFeedback.objects.select_related('user').order_by('-id')[offset:offset + 21]
        items = [{'id': f.pk, 'username': f.user.username, 'category': f.category,
            'rating': f.rating, 'description': f.description, 'contact': f.contact,
            'created_at': f.created_at.isoformat(), 'image_count': len(f.image_keys)} for f in rows]
        response = JsonResponse({'items': items[:20], 'has_more': len(items) > 20})
        response['Cache-Control'] = 'no-store'
        return response
    category = request.POST.get('category', '')
    description = request.POST.get('description', '').strip()
    contact = request.POST.get('contact', '').strip()
    try:
        rating = int(request.POST.get('rating', '0'))
        if category not in CATEGORIES or rating not in range(1, 6) or not 1 <= len(description) <= 2000 or len(contact) > 200:
            raise ValueError('请填写问题类型、1–5 分体验评价和 2000 字以内的问题描述，联系方式最多 200 字')
        images = validate_images(request.FILES.getlist('images'))
    except ValueError as exc:
        return JsonResponse({'message': str(exc)}, status=400)
    keys = []
    try:
        for image in images:
            key = f'private/feedback/{uuid.uuid4().hex}.jpg'
            keys.append(key)
            put_object_with_key(image, key, 'image/jpeg', private=True)
        with transaction.atomic():
            feedback = CustomerFeedback.objects.create(user_id=request.user_id, category=category,
                rating=rating, description=description, contact=contact, image_keys=keys)
    except Exception:
        logger.exception('反馈保存失败')
        for key in keys:
            try:
                delete_object(key)
            except Exception:
                logger.exception('反馈图片清理失败')
        return JsonResponse({'message': '反馈保存失败，请稍后重试'}, status=503)
    return JsonResponse({'id': feedback.pk}, status=201)


@require_GET
@login_checker
def image(request, feedback_id, index):
    if not is_platform_administrator(request.user_id):
        return JsonResponse({'message': '仅平台管理员可以查看反馈图片'}, status=403)
    feedback = CustomerFeedback.objects.filter(pk=feedback_id).first()
    if not feedback or index >= len(feedback.image_keys):
        return JsonResponse({'message': '图片不存在'}, status=404)
    try:
        response = HttpResponse(get_object_bytes(feedback.image_keys[index], MAX_IMAGE_BYTES), content_type='image/jpeg')
    except Exception:
        logger.exception('反馈图片读取失败')
        return JsonResponse({'message': '图片读取失败，请稍后重试'}, status=503)
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
