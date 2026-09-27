import json

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.access import authenticated, public
from events.models import Event
from records import services
from records.models import SignedRecord


@public
def keys(request):
    """The public keys, in JWK form. Cache this and verify records offline."""
    response = JsonResponse(services.jwks())
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=3600"
    return response


@public
def detail(request, public_id):
    record = get_object_or_404(SignedRecord.objects.select_related("key", "event"), public_id=public_id)
    return render(request, "records/detail.html", {
        "record": record, "valid": services.verify(record),
        "document": json.dumps(services.export(record), indent=2, ensure_ascii=False),
    })


@public
def certificate(request, public_id):
    record = get_object_or_404(SignedRecord.objects.select_related("event", "key"), public_id=public_id,
                               kind__in=[SignedRecord.Kind.CERTIFICATE, SignedRecord.Kind.JUDGE])
    return render(request, "records/certificate.html", {
        "record": record, "p": record.payload, "valid": services.verify(record),
        "verify_url": request.build_absolute_uri(f"/records/{record.public_id}/"),
    })


@public
def verify(request):
    """Paste a record to check it against this portal's keys."""
    outcome = None
    text = ""
    if request.method == "POST":
        text = request.POST.get("document", "")
        try:
            outcome = services.verify_document(json.loads(text))
        except ValueError:
            outcome = (False, "that is not JSON")
    return render(request, "records/verify.html", {"outcome": outcome, "text": text})


@authenticated
def my_certificate(request, slug):
    event = get_object_or_404(Event, slug=slug)
    record = services.certificate_for(event, request.user)
    if record is None:
        raise Http404("Certificates are available to participants once results are published.")
    return redirect("records:certificate", public_id=record.public_id)
