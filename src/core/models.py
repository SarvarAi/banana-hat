from django.db import models

from core.ids import new_public_id


class PublicIdMixin(models.Model):
    """Adds a prefixed, random `public_id` used in URLs and the API.

    The id is minted when the instance is constructed rather than in save(), so
    bulk_create, which never calls save(), cannot produce rows without one.
    """

    PUBLIC_ID_PREFIX = "obj"

    public_id = models.CharField(max_length=40, unique=True, editable=False)
    external_id = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="The id this row had in the file it was imported from, kept for lossless export.",
    )

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Rows loaded from the database already have one. A deferred field is absent
        # from __dict__, and touching it here would cost a query, so look, don't read.
        if "public_id" in self.__dict__ and not self.__dict__["public_id"]:
            self.public_id = new_public_id(self.public_id_prefix())

    @property
    def interchange_id(self) -> str:
        """The id to write in an export: the original one if this row was imported."""
        return self.external_id or self.public_id

    def public_id_prefix(self) -> str:
        return self.PUBLIC_ID_PREFIX


class RateLimitCounter(models.Model):
    """One fixed-window counter for `core.ratelimit`.

    Lives in the database rather than a per-process cache so every web worker counts
    against the same number. The key is a digest, never a raw address.
    """

    key = models.CharField(max_length=64)
    window_start = models.BigIntegerField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "window_start"], name="ratelimit_key_window"),
        ]
        indexes = [models.Index(fields=["window_start"])]

    def __str__(self):
        return f"{self.key[:8]}… @{self.window_start}: {self.count}"
