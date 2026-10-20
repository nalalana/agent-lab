# 新闻热点

部署时在后端目录、使用目标环境配置执行 `python manage.py migrate`，创建新闻表。
新闻初始为空，不自动发布示例新闻。

- `GET /api/news`：公开列表，每页 20 条，按发布顺序倒序；将 `next_cursor` 作为下一页的 `before` 参数。
- `GET /api/news/<id>`：公开详情。
- `GET /api/news/access`：登录用户的 `can_publish` 权限。
- `POST /api/news`：平台用户角色为 `admin` 或 `root` 时可发布；请求包含 `title`（120 字）、`summary`（300 字）、`content`（50000 字）。均必填，发布时间及发布者由服务端设置。

前端入口：个人中心 → 发布新闻热点。正文支持文字与图片，拖入、粘贴或选择图片可插入光标位置；不执行粘贴的 HTML。
图片接口 `POST /api/news/images` 使用 `file` 字段，只允许平台管理员上传有效的 JPG/PNG/GIF/WEBP（单张 10MB、2500 万像素以内）。复用 COS 与图片审核配置。
发布时可传 `body` 数组，文本块为 `{type: "text", text: "..."}`，图片块为 `{type: "image", key: "news/..."}`。最多 20 张图片，必须属于当前发布者且通过审核；数据库仅存图片 key，详情接口重新生成访问 URL。旧纯文本新闻继续兼容。
上线前执行 `python manage.py migrate`，应用 `news.0002_newshotspot_body`。
企业成员的管理权限不授予平台新闻发布权限。

验证：`python manage.py test apps.news --settings=config.settings.security_tests --noinput`。
