"""
/backend/common/utils/ip_location_utils.py
IP 地理位置工具类
"""
import logging
import os
from typing import Optional, Tuple
from pathlib import Path

logger = logging.getLogger('touming')

# 尝试导入 iputil，如果失败则使用备用方案
try:
    import iputil
    IPUTIL_AVAILABLE = True
except ImportError:
    IPUTIL_AVAILABLE = False
    logger.warning("[ip-location] iputil 未安装，将使用备用方案")


def get_ip_location(ip: Optional[str]) -> Optional[str]:
    """
    根据 IP 地址获取地理位置信息
    
    Args:
        ip: IP 地址字符串
        
    Returns:
        地理位置字符串，格式：国家 省份 城市，例如："中国 广东 深圳"
        如果无法获取，返回 None
    """
    if not ip:
        return None
    
    # 过滤本地 IP
    if ip in ['127.0.0.1', 'localhost', '::1'] or ip.startswith('172.') or ip.startswith('192.168.') or ip.startswith('10.'):
        return "本地网络"
    
    # 方法1：使用 iputil（推荐，纯本地）
    if IPUTIL_AVAILABLE:
        try:
            return _get_location_by_iputil(ip)
        except Exception as e:
            logger.warning(f"[ip-location] iputil 查询失败 ip={ip} err={e}")
    
    # 方法2：使用在线 API（备用方案）
    try:
        return _get_location_by_api(ip)
    except Exception as e:
        logger.warning(f"[ip-location] API 查询失败 ip={ip} err={e}")
        return None


def _get_location_by_iputil(ip: str) -> Optional[str]:
    """
    使用 iputil 本地数据库查询
    
    Args:
        ip: IP 地址
        
    Returns:
        地理位置字符串
    """
    try:
        # iputil 会自动下载数据库文件（首次使用时）
        # 也可以手动指定数据库文件路径
        BASE_DIR = Path(__file__).resolve().parents[2]
        db_path = BASE_DIR / 'backend' / 'data' / 'ip2region.db'
        
        # 如果数据库文件存在，使用自定义路径
        if db_path.exists():
            iputil.set_region_source(data_file=str(db_path))
        elif (BASE_DIR / 'data' / 'ip2region.db').exists():
            iputil.set_region_source(data_file=str(BASE_DIR / 'data' / 'ip2region.db'))
        
        # 查询 IP
        region = iputil.get_region(ip)
        
        if region:
            # iputil 返回格式：国家|区域|省份|城市|ISP
            # 例如："中国|0|广东|深圳|联通"
            parts = region.split('|')
            if len(parts) >= 4:
                country = parts[0] if parts[0] != '0' else ''
                province = parts[2] if parts[2] != '0' else ''
                city = parts[3] if parts[3] != '0' else ''
                
                # 组合地址（不包含 ISP）
                location_parts = []
                if country and country != '内网IP':
                    location_parts.append(country)
                if province:
                    location_parts.append(province)
                if city:
                    location_parts.append(city)
                
                if location_parts:
                    return ' '.join(location_parts)
        
        return None
    except Exception as e:
        logger.error(f"[ip-location] iputil 查询异常 ip={ip} err={e}")
        return None


def _get_location_by_api(ip: str) -> Optional[str]:
    """
    使用在线 API 查询（备用方案）
    
    使用免费的 ip-api.com API（无需注册，但有请求限制）
    
    Args:
        ip: IP 地址
        
    Returns:
        地理位置字符串
    """
    try:
        import requests
        
        # 使用 ip-api.com（免费，无需 API Key）
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city"
        
        response = requests.get(url, timeout=2)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                country = data.get('country', '')
                region = data.get('regionName', '')
                city = data.get('city', '')
                
                location_parts = []
                if country:
                    location_parts.append(country)
                if region:
                    location_parts.append(region)
                if city:
                    location_parts.append(city)
                
                if location_parts:
                    return ' '.join(location_parts)
        
        return None
    except Exception as e:
        logger.error(f"[ip-location] API 查询异常 ip={ip} err={e}")
        return None


def format_ip_location(ip: Optional[str]) -> str:
    """
    格式化 IP 地址和地理位置信息
    
    Args:
        ip: IP 地址
        
    Returns:
        格式化的字符串，例如："（IP: 172.18.0.1，位置: 广东 深圳）"
    """
    if not ip:
        return ""
    
    location = get_ip_location(ip)
    
    if location:
        return f"（IP: {ip}，位置: {location}）"
    else:
        return f"（IP: {ip}）"
