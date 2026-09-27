from django.contrib import admin
from django.urls import path

from core import views as core_views

admin.site.site_header = "Banana Hat administration"
admin.site.site_title = "Banana Hat admin"

handler403 = "core.views.permission_denied"
handler404 = "core.views.page_not_found"
handler500 = "core.views.server_error"

urlpatterns = [
    path("healthz", core_views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
]
