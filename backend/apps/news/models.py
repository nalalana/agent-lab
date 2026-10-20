from django.db import models


class NewsHotspot(models.Model):
    title = models.CharField(max_length=120)
    summary = models.CharField(max_length=300)
    content = models.TextField()
    body = models.JSONField(default=list, blank=True)
    author = models.ForeignKey('user.User', on_delete=models.SET_NULL, null=True)
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-published_at', '-id']
