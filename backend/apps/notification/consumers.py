"""
/backend/apps/notification/consumers.py
消息通知 WebSocket 消费者（Channels）
"""
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from apps.user.models import User
from apps.notification.models import Notification
from apps.notification.services import NotificationService

logger = logging.getLogger('touming')


class NotificationConsumer(AsyncWebsocketConsumer):
    """通知 WebSocket 消费者"""
    
    async def connect(self):
        """WebSocket 连接建立"""
        # 从 scope 中获取用户信息（由中间件设置）
        self.user_id = self.scope.get('user_id')
        
        if not self.user_id:
            logger.warning(f"[notification-websocket] 连接被拒绝：未找到 user_id scope={self.scope.get('path', 'unknown')}")
            await self.close(code=4001)  # 4001 = 未授权
            return
        
        # 创建用户专属的 channel group
        self.room_group_name = f'notification_{self.user_id}'
        
        # 加入 group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        
        await self.accept()
        logger.info(f"[notification-websocket] 连接建立 user_id={self.user_id}")
    
    async def disconnect(self, close_code):
        """WebSocket 连接断开"""
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
        logger.info(f"[notification-websocket] 连接断开 user_id={self.user_id} close_code={close_code}")
    
    async def receive(self, text_data):
        """接收客户端消息"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'ping':
                # 心跳检测
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': data.get('timestamp')
                }))
            elif message_type == 'get_unread_count':
                # 获取未读数量
                count = await self.get_unread_count_async()
                await self.send(text_data=json.dumps({
                    'type': 'unread_count',
                    'count': count
                }))
        except json.JSONDecodeError:
            logger.warning(f"[notification-websocket] 接收消息格式错误 user_id={self.user_id}")
        except Exception as e:
            logger.error(f"[notification-websocket] 处理消息失败 user_id={self.user_id} err={e}")
    
    async def notification_message(self, event):
        """接收通知消息（从 channel layer）"""
        message = event['message']
        await self.send(text_data=json.dumps(message))
    
    @database_sync_to_async
    def get_unread_count_async(self):
        """异步获取未读通知数量"""
        try:
            user = User.objects.get(id=self.user_id)
            return NotificationService.get_unread_count(user)
        except User.DoesNotExist:
            return 0


def send_notification_via_websocket(user_id: int, notification_data: dict):
    """
    通过 WebSocket 发送通知（同步函数，在任务中调用）
    
    Args:
        user_id: 用户ID
        notification_data: 通知数据字典
    """
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    
    channel_layer = get_channel_layer()
    if channel_layer:
        group_name = f'notification_{user_id}'
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                'type': 'notification_message',
                'message': {
                    'type': 'notification',
                    'data': notification_data
                }
            }
        )
        logger.info(f"[notification-websocket] 发送通知到 WebSocket user_id={user_id}")
    else:
        logger.warning(f"[notification-websocket] Channel layer 未配置，无法发送 WebSocket 通知")
