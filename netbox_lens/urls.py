from django.urls import path

from . import views

app_name = "netbox_lens"

urlpatterns = [
    path("search/", views.LensSearchView.as_view(), name="search"),
    path("mac-history/", views.LensMacHistoryView.as_view(), name="mac_history"),
    path("arp-history/", views.LensArpHistoryView.as_view(), name="arp_history"),
    path("interfaces/", views.LensInterfaceSearchView.as_view(), name="interface_search"),
    path(
        "down-ports/",
        views.LensInterfaceSearchView.as_view(page_title="Down Ports", default_filters={"admin": "down"}),
        name="down_ports",
    ),
    path("nac-status/", views.LensNacStatusView.as_view(), name="nac_status"),
    path("status/", views.LensStatusView.as_view(), name="status"),
    path("discover/<int:pk>/", views.LensDiscoverView.as_view(), name="discover"),
]
