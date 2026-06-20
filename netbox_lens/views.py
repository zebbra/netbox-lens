from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.shortcuts import render
from django.views import View
from utilities.htmx import htmx_partial

from .backends import get_backends
from .forms import NodeSearchForm


class LensStatusView(View):
    def get(self, request):
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backends = get_backends(config)
        statuses = []
        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(b.status): b for b in backends}
            for future in as_completed(futures):
                statuses.append(future.result())
        return render(request, "netbox_lens/status.html", {
            "statuses": statuses,
            "config_error": None if backends else (
                "No backends configured. Add at least one backend to "
                "PLUGINS_CONFIG['netbox_lens']['backends']."
            ),
        })


class LensSearchView(View):
    def get(self, request):
        form = NodeSearchForm(request.GET or None)
        context = {"form": form}

        if form.is_valid():
            query = form.cleaned_data["q"]
            archived = form.cleaned_data.get("archived", False)
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)

            if not backends:
                context["config_error"] = (
                    "No backends are configured. Add at least one backend to "
                    "PLUGINS_CONFIG['netbox_lens']['backends']."
                )
            else:
                results = [None] * len(backends)
                with ThreadPoolExecutor() as executor:
                    futures = {
                        executor.submit(b.search, query, archived): i
                        for i, b in enumerate(backends)
                    }
                    for future in as_completed(futures):
                        results[futures[future]] = future.result()

                context["results"] = results
                context["query"] = query

            if htmx_partial(request):
                return render(request, "netbox_lens/search_results.html", context)

        return render(request, "netbox_lens/search.html", context)
