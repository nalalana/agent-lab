"""
健康检查视图
用于 Docker 健康检查和负载均衡器健康检查
"""
from django.http import JsonResponse
from django.db import connection


def health_check(request):
    """
    健康检查端点
    检查数据库连接和应用状态
    """
    try:
        # 检查数据库连接
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        
        return JsonResponse({
            'status': 'healthy',
            'database': 'connected'
        }, status=200)
    except Exception as e:
        return JsonResponse({
            'status': 'unhealthy',
            'database': 'disconnected',
            'error': str(e)
        }, status=503)

