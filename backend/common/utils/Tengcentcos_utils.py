import os
import datetime
import uuid
import base64
from urllib.parse import urlparse, unquote, quote
from qcloud_cos import CosConfig, CosS3Client


def _get_cos_client_and_bucket():
    """从环境变量加载 COS 配置并初始化客户端。"""
    secret_id = os.getenv("COS_SECRET_ID", "").strip()
    secret_key = os.getenv("COS_SECRET_KEY", "").strip()
    region = os.getenv("COS_REGION", "").strip()
    bucket_name = os.getenv("COS_BUCKET", "").strip()
    token = os.getenv("COS_TOKEN") or None
    scheme = os.getenv("COS_SCHEME", "https").strip() or "https"

    missing = []
    if not secret_id:
        missing.append("COS_SECRET_ID")
    if not secret_key:
        missing.append("COS_SECRET_KEY")
    if not region:
        missing.append("COS_REGION")
    if not bucket_name:
        missing.append("COS_BUCKET")
    if missing:
        raise RuntimeError(f"COS 环境变量未配置: {', '.join(missing)}")

    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=token,
        Scheme=scheme,
    )
    client = CosS3Client(config)
    return client, bucket_name

def put_object(file_content: bytes, file_name: str):
    client, bucket_name = _get_cos_client_and_bucket()
    now = datetime.datetime.now()
    key = f'{now.year}/{now.month}/{str(uuid.uuid4())}_{file_name}'
    response = client.put_object(
        Bucket=bucket_name,
        Body=file_content,
        Key=key
    )
    print(response['ETag'])
    return key


def put_object_with_key(file_content: bytes, file_key: str, content_type: str = "", *, private: bool = False):
    client, bucket_name = _get_cos_client_and_bucket()
    kwargs = {
        "Bucket": bucket_name,
        "Body": file_content,
        "Key": file_key,
    }
    if content_type:
        kwargs["ContentType"] = content_type
    if private:
        kwargs["ACL"] = "private"
    response = client.put_object(**kwargs)
    return {
        "key": file_key,
        "etag": response.get("ETag", ""),
    }


def get_object_bytes(file_name: str, max_bytes: int | None = None) -> bytes:
    """Read a private COS object with a hard declared and actual byte limit."""

    client, bucket_name = _get_cos_client_and_bucket()
    response = client.get_object(
        Bucket=bucket_name,
        Key=normalize_cos_key(file_name),
    )
    limit = max(int(max_bytes), 1) if max_bytes is not None else None
    declared_size = int(response.get("Content-Length", -1))
    if limit is not None and declared_size > limit:
        raise ValueError(f"COS object exceeds the configured limit ({limit} bytes)")
    body = response["Body"]
    stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else body
    chunks = []
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if limit is not None and total > limit:
            raise ValueError(f"COS object exceeds the configured limit ({limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def delete_object(file_name: str):
    """Delete one exact normalized COS key; callers authorize the logical path."""

    client, bucket_name = _get_cos_client_and_bucket()
    return client.delete_object(Bucket=bucket_name, Key=normalize_cos_key(file_name))


def get_object(file_name: str) -> str:
    try:
        file_content = get_object_bytes(file_name)
        base64_content = base64.b64encode(file_content).decode('utf-8')
        if file_name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
            return f"data:image/{file_name.split('.')[-1].lower()};base64,{base64_content}"
        else:
            return base64_content
    except Exception as e:
        print(f"获取COS文件失败: {str(e)}")
        raise e


def normalize_cos_key(value: str | None) -> str:
    """
    将前端传来的「完整 presigned URL」或「COS key」规范为只存 key。
    数据库只存 key，下发时再生成 presigned URL；避免存超长 URL 导致 255 长度限制报错。
    若库中存在历史脏数据（如「域名+编码后的完整URL」），会循环提取 path 直到得到纯 key。
    """
    if not value or not value.strip():
        return ""
    result = value.strip()
    while result and (result.startswith("http://") or result.startswith("https://")):
        parsed = urlparse(result)
        result = unquote(parsed.path.lstrip("/"))
    return result


def get_cos_cdn_base_url() -> str:
    return os.getenv("COS_CDN_BASE_URL", "").strip().rstrip("/")


def get_public_cdn_url(file_key: str | None) -> str | None:
    normalized_key = normalize_cos_key(str(file_key)) if file_key else ""
    if not normalized_key:
        return None
    base_url = get_cos_cdn_base_url()
    if not base_url:
        return None
    encoded_key = quote(normalized_key, safe="/-_.~")
    return f"{base_url}/{encoded_key}"

def get_avatar_presigned_url(avatar_path, expires_in: int = 3600):
    """获取头像临时访问URL的公共函数"""
    if not avatar_path:
        return None
    try:
        # 从完整 URL 中提取并规范化文件 key，避免出现 %25 双重编码
        # 示例：/2026/2/a_%E5%9B%BE.png -> 2026/2/a_图.png
        if isinstance(avatar_path, str) and avatar_path.startswith('http'):
            parsed_url = urlparse(avatar_path)
            file_key = parsed_url.path.lstrip('/')
        else:
            file_key = str(avatar_path).lstrip('/')

        file_key = unquote(file_key)
        try:
            from apps.moderation.services import ImageModerationService

            if not ImageModerationService.is_publicly_viewable(file_key):
                return None
        except Exception:
            return None
        return get_public_cdn_url(file_key)
    except Exception:
        return None


def get_moderated_image_presigned_url(file_key: str | None, expires_in: int = 3600):
    if not file_key:
        return None
    normalized_key = normalize_cos_key(str(file_key))
    if not normalized_key:
        return None
    try:
        from apps.moderation.services import ImageModerationService

        if not ImageModerationService.is_publicly_viewable(normalized_key):
            return None
    except Exception:
        return None
    return get_public_cdn_url(normalized_key)


def get_presigned_url(file_key: str, expires_in: int = 3600, method: str = 'GET'):
    try:
        client, bucket_name = _get_cos_client_and_bucket()
        response = client.get_presigned_url(
            Bucket=bucket_name,
            Key=file_key,
            Expired=expires_in,
            Method=method,
            Params={},
            Headers={},
            UseCiEndPoint=False,
            SignHost=None
        )
        return response
    except Exception as e:
        print(f"获取预签名URL失败: {str(e)}")
        raise e


def get_presigned_upload_url(file_key: str, expires_in: int = 3600):
    return get_presigned_url(file_key, expires_in=expires_in, method='PUT')


