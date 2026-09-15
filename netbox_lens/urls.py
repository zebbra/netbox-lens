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
        # Down Ports means "should be up but isn't": admin-enabled (NetBox)
        # yet operationally down (VictoriaMetrics interfaceUpDownState) —
        # not NetBox's admin=down, which is just administratively disabled.
        views.LensInterfaceSearchView.as_view(
            page_title="Down Ports", default_filters={"admin": "up", "oper": "down"}
        ),
        name="down_ports",
    ),
    path("nac-status/", views.LensNacStatusView.as_view(), name="nac_status"),
    path("status/", views.LensStatusView.as_view(), name="status"),
    path("discover/<int:pk>/", views.LensTriggerJobView.as_view(job_method="trigger_discover"), name="discover"),
    path("macsuck/<int:pk>/", views.LensTriggerJobView.as_view(job_method="trigger_macsuck"), name="macsuck"),
    path("arpnip/<int:pk>/", views.LensTriggerJobView.as_view(job_method="trigger_arpnip"), name="arpnip"),
    path("rebuild/<int:pk>/", views.LensRebuildInventoryView.as_view(), name="rebuild_inventory"),
    path("rebuild-now/<int:pk>/", views.LensRebuildNowView.as_view(), name="rebuild_now"),
    path("sync/<int:pk>/", views.LensSyncView.as_view(), name="sync"),
    path("probe/<int:pk>/", views.LensProbeView.as_view(), name="probe"),
    path("update-modules/<int:pk>/", views.LensUpdateModulesView.as_view(), name="update_modules"),
    path("discobox/pause/", views.LensDiscoboxPauseView.as_view(), name="discobox_pause"),
]
