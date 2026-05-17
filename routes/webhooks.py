import logging
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()
log = logging.getLogger("ugoingviral.webhooks")

INSTAGRAM_VERIFY_TOKEN = "ugoingviral_webhook_2026"


@router.get("/api/webhooks/instagram")
async def instagram_webhook_verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == INSTAGRAM_VERIFY_TOKEN:
        log.info("Instagram webhook verified")
        return PlainTextResponse(challenge, status_code=200)

    log.warning("Instagram webhook verification failed (mode=%s)", mode)
    return PlainTextResponse("Forbidden", status_code=403)


@router.post("/api/webhooks/instagram")
async def instagram_webhook_receive(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = (await request.body()).decode("utf-8", errors="replace")
    log.info("Instagram webhook event: %s", payload)
    return {"status": "ok"}
