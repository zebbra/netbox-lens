from django.urls import path

from . import views

app_name = "netbox_lens"

urlpatterns = [
    path("search/", views.LensSearchView.as_view(), name="search"),
    path("status/", views.LensStatusView.as_view(), name="status"),
    path("discover/<int:pk>/", views.LensDiscoverView.as_view(), name="discover"),
]
