from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.company.tencent_ess_callbacks import (
    CallbackValidationError,
    process_tencent_ess_callback,
)


@csrf_exempt
def tencent_ess_callback(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)
    try:
        process_tencent_ess_callback(
            raw_body=request.body,
            content_signature=request.META.get('HTTP_CONTENT_SIGNATURE', ''),
        )
    except CallbackValidationError as exc:
        return JsonResponse(
            {'result': 0, 'code': exc.code, 'message': '回调校验失败'},
            status=exc.status_code,
        )
    return JsonResponse({'result': 1, 'message': 'ok'}, status=200)
