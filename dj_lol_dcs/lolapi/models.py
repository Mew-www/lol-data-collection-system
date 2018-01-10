from django.db import models


class GameVersion(models.Model):
    id = models.CharField(primary_key=True, max_length=255)
