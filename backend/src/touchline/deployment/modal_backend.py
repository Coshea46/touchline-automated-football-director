"""Dedicated CPU website deployment; import through Modal only when deploying.

The image builder contains no deployment action. Named secret/volume references
are lazy and must already exist; this module never provisions cloud resources.
The existing GPU inference application and its resource names are untouched.
"""

import os

import modal

from touchline.deployment.image import ADMISSION_DATABASE, STATE_DIRECTORY, build_backend_image
from touchline.paths import BACKEND_ROOT


APP_NAME = "touchline-backend"
ENDPOINT_LABEL = "touchline-backend"
SECRET_NAME = os.environ.get("MATCHPULSE_BACKEND_SECRET_NAME") or "touchline-backend-config"
STATE_VOLUME_NAME = "touchline-backend-state"
INFERENCE_AUTH_SECRET_NAME = "matchpulse-inference-replay-auth"

app = modal.App(APP_NAME)
state_volume = modal.Volume.from_name(STATE_VOLUME_NAME, create_if_missing=False)
configuration = modal.Secret.from_name(SECRET_NAME)
inference_authentication = modal.Secret.from_name(INFERENCE_AUTH_SECRET_NAME)
image = (build_backend_image(modal, BACKEND_ROOT) if modal.is_local() else
         modal.Image.debian_slim(python_version="3.12"))


@app.function(image=image, secrets=[configuration, inference_authentication], volumes={STATE_DIRECTORY: state_volume},
              cpu=(0.25, 4), memory=(512, 4096), timeout=3600, startup_timeout=120,
              min_containers=1, max_containers=1, buffer_containers=0,
              scaledown_window=60, routing_region="eu-west", include_source=False)
@modal.concurrent(max_inputs=100, target_inputs=50)
@modal.asgi_app(label=ENDPOINT_LABEL)
def public_backend():
    # The SDK requires a synchronous, nullary ASGI factory. The returned app
    # handles requests asynchronously and owns its lifespan/session cleanup.
    from touchline.hosted.admission import AdmissionPolicy
    from touchline.hosted.app import create_app

    # The existing inference app uses its own server-side token name. Reuse that
    # secret without redeploying workers or exposing its value to the browser.
    if not os.environ.get("MATCHPULSE_MODAL_WS_TOKEN"):
        token = os.environ.get("MATCHPULSE_REPLAY_WS_TOKEN")
        if token:
            os.environ["MATCHPULSE_MODAL_WS_TOKEN"] = token

    # Load the durable admission state before constructing the policy. Its commit
    # callback persists every paid reservation before provider work may begin.
    state_volume.reload()
    policy = AdmissionPolicy(db_path=ADMISSION_DATABASE, commit=state_volume.commit,
                             env=os.environ)
    return create_app(admission=policy)
