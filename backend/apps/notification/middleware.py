"""
/backend/apps/notification/middleware.py
WebSocket 认证中间件
"""
import logging
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async

logger = logging.getLogger('touming')


class WebSocketTokenAuthMiddleware(BaseMiddleware):
    """
    WebSocket Token 认证中间件
    
    从查询参数或 headers 中获取 token，验证后设置 user_id 到 scope
    """
    
    async def __call__(self, scope, receive, send):
        # 只处理 WebSocket 连接
        if scope['type'] != 'websocket':
            return await super().__call__(scope, receive, send)
        
        # 从查询参数获取 token
        query_string = scope.get('query_string', b'').decode()
        token = None
        
        # 解析查询参数
        if query_string:
            try:
                from urllib.parse import parse_qs
                params = parse_qs(query_string)
                # parse_qs 返回的是列表，取第一个值
                token = params.get('token', [None])[0]
            except Exception as e:
                logger.warning(f"[websocket-auth] 解析查询参数失败 query_string={query_string} err={e}")
                # 备用方案：简单字符串解析
                if 'token=' in query_string:
                    token = query_string.split('token=')[1].split('&')[0]
        
        # 如果查询参数中没有，尝试从 headers 获取
        if not token:
            headers = dict(scope.get('headers', []))
            # 查找 access-token 或 authorization header
            access_token_header = headers.get(b'access-token') or headers.get(b'access_token')
            auth_header = headers.get(b'authorization') or headers.get(b'Authorization')
            
            if access_token_header:
                token = access_token_header.decode() if isinstance(access_token_header, bytes) else access_token_header
            elif auth_header:
                auth_value = auth_header.decode() if isinstance(auth_header, bytes) else auth_header
                token = auth_value.replace('Bearer ', '').replace('bearer ', '')
        
        # 验证 token
        if token:
            # 延迟导入：避免在 ASGI 模块导入阶段触发 Django model 导入
            def _check(token_value: str):
                from common.utils.Login_utils import check_token
                return check_token(token_value)

            payload = await database_sync_to_async(_check)(token)
            if payload and payload.get('user_id'):
                scope['user_id'] = payload['user_id']
                logger.info(f"[websocket-auth] 认证成功 user_id={scope['user_id']}")
            else:
                logger.warning(f"[websocket-auth] Token 验证失败")
                scope['user_id'] = None
        else:
            logger.warning(f"[websocket-auth] 未找到 token")
            scope['user_id'] = None
        
        return await super().__call__(scope, receive, send)
