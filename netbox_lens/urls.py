from django.urls import path

from . import views

app_name = "netbox_lens"

urlpatterns = [
    path("search/", views.LensSearchView.as_view(), name="search"),
    path("search/history/", views.LensSearchHistoryView.as_view(), name="search_history"),
    path("mac-history/", views.LensMacHistoryView.as_view(), name="mac_history"),
    path("arp-history/", views.LensArpHistoryView.as_view(), name="arp_history"),
    path("interfaces/", views.LensInterfaceSearchView.as_view(), name="interface_search"),
    # Down Ports = access ports free for reuse (no MAC for N days), from
    # Netdisco's portfree report — not "admin up but oper down".
    path("down-ports/", views.LensDownPortsView.as_view(), name="down_ports"),
    path("nac-status/", views.LensNacStatusView.as_view(), name="nac_status"),
    path("status/", views.LensStatusView.as_view(), name="status"),
    path("discover/<int:pk>/", views.LensTriggerJobView.as_view(job_method="trigger_discover"), name="discover"),
    path("macsuck/<int:pk>/", views.LensTriggerJobView.as_view(job_method="trigger_macsuck"), name="macsuck"),
    path("arpnip/<int:pk>/", views.LensTriggerJobView.as_view(job_method="trigger_arpnip"), name="arpnip"),
    path("rebuild/<int:pk>/", views.LensRebuildInventoryView.as_view(), name="rebuild_inventory"),
    path("rebuild-now/<int:pk>/", views.LensRebuildNowView.as_view(), name="rebuild_now"),
    path("refresh-ip/<int:pk>/", views.LensRefreshIpView.as_view(), name="refresh_ip"),
    path("refresh-ips/", views.LensRefreshIpsView.as_view(), name="refresh_ips"),
    path("bulk-sync/", views.LensBulkSyncView.as_view(), name="bulk_sync"),
    path("sync/<int:pk>/", views.LensSyncView.as_view(), name="sync"),
    path("probe/<int:pk>/", views.LensProbeView.as_view(), name="probe"),
    path("update-modules/<int:pk>/", views.LensUpdateModulesView.as_view(), name="update_modules"),
    path("discobox/pause/", views.LensDiscoboxPauseView.as_view(), name="discobox_pause"),
    path("interface/<int:pk>/nodes-data/", views.LensInterfaceNodesDataView.as_view(), name="interface_nodes_data"),
    path("device/<int:pk>/stats-data/", views.LensDeviceStatsDataView.as_view(), name="device_stats_data"),
    path("interface/<int:pk>/port-data/", views.LensInterfacePortDataView.as_view(), name="interface_port_data"),
    path("device/<int:pk>/panel-data/", views.LensDevicePanelDataView.as_view(), name="device_panel_data"),
    path("device/<int:pk>/jobs-data/", views.LensDeviceJobsDataView.as_view(), name="device_jobs_data"),
    path("device/<int:pk>/macarp-data/", views.LensDeviceMacArpDataView.as_view(), name="device_macarp_data"),
]
