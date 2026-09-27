from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Accounts"

    def ready(self):
        from accounts import schema  # noqa: F401 (registers the OpenAPI security scheme)
