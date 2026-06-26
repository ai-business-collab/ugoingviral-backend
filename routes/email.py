"""
Auto Email Svar — UgoingViral
==============================
Håndterer:
- IMAP forbindelse til email konto
- Automatisk AI-genererede svar
- Email templates
- Send email via SMTP
- System emails (velkomst, notifikationer)
"""

from fastapi import APIRouter, HTTPException, Request
from typing import Optional, List
import asyncio, logging, os, json
from datetime import datetime
from services.store import store, save_store, add_log

logger = logging.getLogger("ugv.email")

router = APIRouter()


# ── Email indstillinger ───────────────────────────────────────────────────────

@router.get("/api/email/settings")
def get_email_settings():
    s = store.get("email_settings", {})
    # Masker password
    safe = {k: ("••••••" if "pass" in k.lower() else v) for k, v in s.items()}
    return safe

@router.post("/api/email/settings")
async def save_email_settings(req: Request):
    d = await req.json()
    if "email_settings" not in store:
        store["email_settings"] = {}
    for k, v in d.items():
        if v and "••••" not in str(v):
            store.get("email_settings", {})[k] = v
    store.get("email_settings", {})["active"] = d.get("active", False)
    save_store()
    return {"status": "ok"}

@router.post("/api/email/test")
async def test_email_connection():
    s = store.get("email_settings", {})
    host = s.get("imap_host", "")
    port = int(s.get("imap_port", 993))
    user = s.get("email_user", "")
    pwd = s.get("email_pass", "")
    if not host or not user or not pwd:
        return {"status": "error", "message": "Udfyld IMAP host, email og adgangskode"}
    try:
        import imaplib
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, pwd)
        conn.logout()
        add_log("✅ Email forbindelse OK", "success")
        return {"status": "ok", "message": "Forbundet!"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}


# ── Hent emails ───────────────────────────────────────────────────────────────

@router.get("/api/email/inbox")
async def get_inbox(limit: int = 20):
    s = store.get("email_settings", {})
    if not s.get("email_user"):
        return {"emails": [], "error": "Email ikke konfigureret"}
    try:
        emails = await _fetch_emails(s, limit)
        return {"emails": emails}
    except Exception as e:
        return {"emails": [], "error": str(e)[:100]}

async def _fetch_emails(s: dict, limit: int = 20) -> list:
    import imaplib, email
    from email.header import decode_header
    loop = asyncio.get_event_loop()
    
    def _sync_fetch():
        host = s.get("imap_host", "imap.gmail.com")
        port = int(s.get("imap_port", 993))
        user = s.get("email_user", "")
        pwd = s.get("email_pass", "")
        
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, pwd)
        conn.select("INBOX")
        
        _, msgs = conn.search(None, "ALL")
        ids = msgs[0].split()
        ids = ids[-limit:]  # Seneste X emails
        
        result = []
        for eid in reversed(ids):
            _, data = conn.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            
            # Decode subject
            subj_raw = msg.get("Subject", "")
            subj_parts = decode_header(subj_raw)
            subject = ""
            for part, enc in subj_parts:
                if isinstance(part, bytes):
                    subject += part.decode(enc or "utf-8", errors="replace")
                else:
                    subject += str(part)
            
            # Decode sender
            sender = msg.get("From", "")
            
            # Hent body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        try:
                            body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            break
                        except: pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                except: pass
            
            result.append({
                "id": eid.decode(),
                "subject": subject[:100],
                "sender": sender[:100],
                "date": msg.get("Date", ""),
                "body": body[:500],
                "has_auto_reply": False
            })
        
        conn.logout()
        return result
    
    return await loop.run_in_executor(None, _sync_fetch)


# ── AI auto-svar ──────────────────────────────────────────────────────────────

@router.post("/api/email/generate-reply")
async def generate_email_reply(req: Request):
    d = await req.json()
    subject = d.get("subject", "")
    body = d.get("body", "")
    sender = d.get("sender", "")
    
    s = store.get("email_settings", {})
    tone = s.get("reply_tone", "professionel og venlig")
    instructions = s.get("custom_instructions", "")
    language = s.get("reply_language", "da")
    
    prompt = f"""Du skal skrive et email-svar på {language}.

Tone: {tone}
{f'Instruktioner: {instructions}' if instructions else ''}

Modtaget email fra: {sender}
Emne: {subject}
Besked: {body[:800]}

Skriv et kort, professionelt og venligt svar. Svar direkte — ingen forklaring af hvad du gør."""

    # Kald AI
    reply = await _call_ai_for_email(prompt)
    return {"reply": reply}

async def _call_ai_for_email(prompt: str) -> str:
    s = store.get("settings", {})
    
    if s.get("anthropic_key") and "••••" not in s["anthropic_key"]:
        try:
            async with __import__("httpx").AsyncClient() as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": s["anthropic_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-3-5-haiku-20241022", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]},
                    timeout=30
                )
                r.raise_for_status()
                return r.json()["content"][0]["text"]
        except: pass
    
    if s.get("openai_key") and "••••" not in s["openai_key"]:
        try:
            async with __import__("httpx").AsyncClient() as c:
                r = await c.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {s['openai_key']}"},
                    json={"model": "gpt-4o-mini", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]},
                    timeout=30
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except: pass
    
    return "Tak for din besked. Vi vender tilbage hurtigst muligt.\n\nMed venlig hilsen"


# ── Send email ────────────────────────────────────────────────────────────────

@router.post("/api/email/send")
async def send_email(req: Request):
    d = await req.json()
    to_addr = d.get("to", "")
    subject = d.get("subject", "")
    body = d.get("body", "")
    
    if not to_addr or not body:
        return {"status": "error", "message": "Mangler modtager eller besked"}
    
    s = store.get("email_settings", {})
    smtp_host = s.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(s.get("smtp_port", 587))
    user = s.get("email_user", "")
    pwd = s.get("email_pass", "")
    
    if not user or not pwd:
        return {"status": "error", "message": "Email ikke konfigureret"}
    
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(user, pwd)
            server.send_message(msg)
        
        add_log(f"📧 Email sendt til {to_addr[:30]}", "success")
        return {"status": "ok", "message": "Email sendt!"}
    except Exception as e:
        add_log(f"❌ Email fejl: {str(e)[:80]}", "error")
        return {"status": "error", "message": str(e)[:100]}


# ── Email templates ───────────────────────────────────────────────────────────

@router.get("/api/email/templates")
def get_templates():
    return {"templates": store.get("email_templates", _default_templates())}

@router.post("/api/email/templates")
async def save_template(req: Request):
    d = await req.json()
    if "email_templates" not in store:
        store["email_templates"] = _default_templates()
    # Tilføj eller opdater
    templates = store.get("email_templates", {})
    existing = next((t for t in templates if t["id"] == d.get("id")), None)
    if existing:
        existing.update(d)
    else:
        d["id"] = f"tmpl_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        templates.append(d)
    save_store()
    return {"status": "ok", "template": d}

@router.delete("/api/email/templates/{tid}")
def delete_template(tid: str):
    store["email_templates"] = [t for t in store.get("email_templates", []) if t["id"] != tid]
    save_store()
    return {"status": "deleted"}

def _default_templates():
    return [
        {"id": "tmpl_order", "name": "Ordre bekræftelse", "subject": "Din ordre er modtaget 🎉", "body": "Kære [navn],\n\nTak for din ordre! Vi behandler den nu og sender den snarest.\n\nMed venlig hilsen"},
        {"id": "tmpl_support", "name": "Support svar", "subject": "Re: [emne]", "body": "Kære [navn],\n\nTak for din henvendelse. Vi kigger på det og vender tilbage inden for 24 timer.\n\nMed venlig hilsen"},
        {"id": "tmpl_follow", "name": "Follow-up", "subject": "Opfølgning fra [firma]", "body": "Kære [navn],\n\nVi ville høre om alt er i orden? Har du spørgsmål er du altid velkommen til at skrive.\n\nMed venlig hilsen"},
    ]


# ── System email (velkomst, notifikationer) ───────────────────────────────────

def _system_smtp_cfg() -> dict:
    return {
        "host": os.getenv("SYSTEM_SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("SYSTEM_SMTP_PORT", "587")),
        "user": os.getenv("SYSTEM_SMTP_USER", ""),
        "password": os.getenv("SYSTEM_SMTP_PASS", ""),
        "from_name": os.getenv("SYSTEM_SMTP_FROM_NAME", "UgoingViral"),
    }


def send_system_email(to_addr: str, subject: str, html_body: str) -> bool:
    # Try SendGrid first (preferred)
    sg_key = os.getenv("SENDGRID_API_KEY", "")
    if sg_key:
        from_email = os.getenv("SENDGRID_FROM", "noreply@ugoingviral.com")
        try:
            import httpx
            r = httpx.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {sg_key}", "Content-Type": "application/json"},
                json={
                    "personalizations": [{"to": [{"email": to_addr}]}],
                    "from": {"email": from_email, "name": "UgoingViral"},
                    "subject": subject,
                    "content": [{"type": "text/html", "value": html_body}],
                },
                timeout=15,
            )
            if r.status_code in (200, 202):
                logger.info("SendGrid → %s · subject=%r · status=%d", to_addr, subject, r.status_code)
                return True
            # Surface why the send failed instead of silently falling through.
            logger.error(
                "SendGrid send failed → to=%s from=%s subject=%r status=%d body=%s",
                to_addr, from_email, subject, r.status_code, (r.text or "")[:500],
            )
        except Exception as e:
            logger.exception("SendGrid send raised an exception for %s: %s", to_addr, e)
        # Fall through to SMTP only if explicitly configured.

    cfg = _system_smtp_cfg()
    if not cfg["user"] or not cfg["password"]:
        logger.error("send_system_email: no SMTP fallback configured (SENDGRID_API_KEY=%s)", "set" if sg_key else "missing")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['from_name']} <{cfg['user']}>"
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(cfg["host"], cfg["port"]) as srv:
            srv.starttls()
            srv.login(cfg["user"], cfg["password"])
            srv.send_message(msg)
        logger.info("SMTP → %s · subject=%r · ok", to_addr, subject)
        return True
    except Exception as e:
        logger.exception("SMTP send failed for %s: %s", to_addr, e)
        add_log(f"❌ System email fejl: {str(e)[:80]}", "error")
        return False


def send_welcome_email(to_addr: str, name: str = "") -> bool:
    first = (name or to_addr.split("@")[0]).split(" ")[0].capitalize()
    subject = f"Welcome to UgoingViral, {first}! 🚀"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{margin:0;padding:0;background:#080c18;font-family:'Helvetica Neue',Arial,sans-serif}}
  .wrap{{max-width:580px;margin:40px auto;background:#0d1526;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,.07)}}
  .header{{background:linear-gradient(135deg,#00e5ff22,#7c3aed22);padding:40px 40px 32px;text-align:center;border-bottom:1px solid rgba(255,255,255,.07)}}
  .logo{{font-size:22px;font-weight:800;color:#f0f4f8;letter-spacing:-0.5px}}
  .logo span{{color:#00e5ff}}
  .hero{{font-size:28px;font-weight:800;color:#f0f4f8;margin:16px 0 8px}}
  .sub{{font-size:15px;color:#7d8fa3;line-height:1.5}}
  .body{{padding:32px 40px}}
  .credits-box{{background:rgba(0,229,255,.06);border:1px solid rgba(0,229,255,.15);border-radius:12px;padding:20px 24px;margin:24px 0;text-align:center}}
  .credits-num{{font-size:36px;font-weight:800;color:#00e5ff}}
  .credits-label{{font-size:13px;color:#7d8fa3;margin-top:4px}}
  .steps{{margin:24px 0}}
  .step{{display:flex;align-items:flex-start;gap:14px;margin-bottom:16px}}
  .step-icon{{font-size:20px;min-width:32px;text-align:center;margin-top:2px}}
  .step-text{{font-size:14px;color:#a0b0c0;line-height:1.5}}
  .step-title{{font-weight:700;color:#f0f4f8;margin-bottom:3px}}
  .cta{{text-align:center;margin:32px 0 8px}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#00e5ff,#7c3aed);color:#fff;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;text-decoration:none;letter-spacing:.3px}}
  .footer{{padding:20px 40px;text-align:center;font-size:11px;color:#3d4f61;border-top:1px solid rgba(255,255,255,.05)}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">Ugoin<span>g</span>Viral</div>
    <div class="hero">Welcome to UgoingViral, {first}! 🎉</div>
    <div class="sub">Your account is live — let's make your social media take off.</div>
  </div>
  <div class="body">
    <div class="credits-box">
      <div class="credits-num">50 Credits</div>
      <div class="credits-label">free on your account — ready to use</div>
    </div>
    <div class="steps">
      <div class="step">
        <div class="step-icon">✍️</div>
        <div class="step-text">
          <div class="step-title">AI content generation</div>
          Generate captions, scripts, hashtags and on-brand videos in seconds — built for Instagram, TikTok, YouTube, Facebook and X.
        </div>
      </div>
      <div class="step">
        <div class="step-icon">📅</div>
        <div class="step-text">
          <div class="step-title">Schedule &amp; auto-post</div>
          Plan a week or a month at a time and let UgoingViral publish to every platform on the right schedule, automatically.
        </div>
      </div>
      <div class="step">
        <div class="step-icon">📈</div>
        <div class="step-text">
          <div class="step-title">Growth analytics &amp; competitor spy</div>
          Track performance across platforms and reverse-engineer the strategies that are working for any competitor you choose.
        </div>
      </div>
    </div>
    <div class="cta">
      <a href="https://ugoingviral.com/app" class="btn">Go to UgoingViral →</a>
    </div>
  </div>
  <div class="footer">
    You're receiving this email because you created an account on ugoingviral.com.<br>
    Questions? Reach us at support@ugoingviral.com
  </div>
</div>
</body>
</html>"""
    return send_system_email(to_addr, subject, html)


def send_verification_email(to_addr: str, name: str, code: str) -> bool:
    first = (name or to_addr.split("@")[0]).split(" ")[0].capitalize()
    subject = f"Your UgoingViral verification code: {code}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{margin:0;padding:0;background:#080c18;font-family:'Helvetica Neue',Arial,sans-serif}}
  .wrap{{max-width:560px;margin:40px auto;background:#0d1526;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,.07)}}
  .header{{background:linear-gradient(135deg,#00e5ff22,#7c3aed22);padding:36px 40px 28px;text-align:center;border-bottom:1px solid rgba(255,255,255,.07)}}
  .logo{{font-size:22px;font-weight:800;color:#f0f4f8;letter-spacing:-0.5px}}
  .logo span{{color:#00e5ff}}
  .hero{{font-size:24px;font-weight:800;color:#f0f4f8;margin:14px 0 8px}}
  .sub{{font-size:14px;color:#7d8fa3;line-height:1.5}}
  .body{{padding:28px 40px 36px;text-align:center}}
  .code-box{{display:inline-block;background:rgba(0,229,255,.08);border:1px solid rgba(0,229,255,.25);border-radius:14px;padding:22px 36px;margin:18px 0}}
  .code{{font-size:36px;font-weight:800;color:#00e5ff;letter-spacing:10px;font-family:'Courier New',monospace}}
  .meta{{font-size:13px;color:#7d8fa3;margin-top:14px;line-height:1.6}}
  .footer{{padding:18px 40px;text-align:center;font-size:11px;color:#3d4f61;border-top:1px solid rgba(255,255,255,.05)}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">Ugoin<span>g</span>Viral</div>
    <div class="hero">Verify your email</div>
    <div class="sub">Hi {first} — enter this 6-digit code on the signup screen to activate your account.</div>
  </div>
  <div class="body">
    <div class="code-box"><div class="code">{code}</div></div>
    <div class="meta">
      This code expires in 30 minutes.<br>
      If you didn't request this, you can safely ignore the email.
    </div>
  </div>
  <div class="footer">
    Please do not reply to this email. For support contact <a href="mailto:support@ugoingviral.com" style="color:#00e5ff">support@ugoingviral.com</a>.
  </div>
</div>
</body>
</html>"""
    return send_system_email(to_addr, subject, html)



def send_password_reset_email(to_addr: str, name: str, reset_url: str) -> bool:
    first = (name or to_addr.split("@")[0]).split(" ")[0].capitalize()
    subject = "Reset your UgoingViral password"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{margin:0;padding:0;background:#080c18;font-family:'Helvetica Neue',Arial,sans-serif}}
  .wrap{{max-width:560px;margin:40px auto;background:#0d1526;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,.07)}}
  .header{{background:linear-gradient(135deg,#00e5ff22,#7c3aed22);padding:36px 40px 28px;text-align:center;border-bottom:1px solid rgba(255,255,255,.07)}}
  .logo{{font-size:22px;font-weight:800;color:#f0f4f8;letter-spacing:-0.5px}}
  .logo span{{color:#00e5ff}}
  .hero{{font-size:24px;font-weight:800;color:#f0f4f8;margin:14px 0 8px}}
  .sub{{font-size:14px;color:#7d8fa3;line-height:1.5}}
  .body{{padding:28px 40px 36px;text-align:center}}
  .cta{{margin:20px 0 8px}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#00e5ff,#7c3aed);color:#fff;font-size:15px;font-weight:700;padding:14px 40px;border-radius:10px;text-decoration:none;letter-spacing:.3px}}
  .link{{font-size:12px;color:#7d8fa3;word-break:break-all;margin-top:16px}}
  .link a{{color:#00e5ff}}
  .meta{{font-size:13px;color:#7d8fa3;margin-top:18px;line-height:1.6}}
  .footer{{padding:18px 40px;text-align:center;font-size:11px;color:#3d4f61;border-top:1px solid rgba(255,255,255,.05)}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">Ugoin<span>g</span>Viral</div>
    <div class="hero">Reset your password</div>
    <div class="sub">Hi {first} — we received a request to reset your UgoingViral password.</div>
  </div>
  <div class="body">
    <div class="cta"><a class="btn" href="{reset_url}">Reset password</a></div>
    <div class="link">Or paste this link into your browser:<br><a href="{reset_url}">{reset_url}</a></div>
    <div class="meta">
      This link expires in 60 minutes and can be used once.<br>
      If you didn't request a password reset, you can safely ignore this email — your password won't change.
    </div>
  </div>
  <div class="footer">
    Please do not reply to this email. For support contact <a href="mailto:support@ugoingviral.com" style="color:#00e5ff">support@ugoingviral.com</a>.
  </div>
</div>
</body>
</html>"""
    return send_system_email(to_addr, subject, html)


def send_payment_confirmation_email(to_addr: str, name: str, plan_name: str, price_dkk: int) -> bool:
    first = (name or to_addr.split("@")[0]).split(" ")[0].capitalize()
    subject = f"Payment confirmed — {plan_name} plan active 🎉"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{margin:0;padding:0;background:#080c18;font-family:Arial,sans-serif}}
  .wrap{{max-width:560px;margin:40px auto;background:#0d1526;border-radius:16px;border:1px solid rgba(255,255,255,.07);overflow:hidden}}
  .hdr{{background:linear-gradient(135deg,rgba(0,229,255,.15),rgba(124,58,237,.15));padding:32px 36px;text-align:center}}
  .logo{{font-size:20px;font-weight:800;color:#f0f4f8}}.logo span{{color:#00e5ff}}
  .body{{padding:28px 36px;color:#a0b0c0;font-size:14px;line-height:1.6}}
  .plan-box{{background:rgba(0,229,255,.06);border:1px solid rgba(0,229,255,.2);border-radius:12px;padding:20px;text-align:center;margin:20px 0}}
  .plan-name{{font-size:24px;font-weight:800;color:#00e5ff}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#00e5ff,#7c3aed);color:#fff;padding:12px 32px;border-radius:10px;text-decoration:none;font-weight:700;margin-top:16px}}
  .footer{{padding:16px 36px;font-size:11px;color:#3d4f61;border-top:1px solid rgba(255,255,255,.05);text-align:center}}
</style></head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="logo">Ugoin<span>g</span>Viral</div>
    <div style="font-size:22px;font-weight:800;color:#f0f4f8;margin-top:12px">Payment confirmed ✅</div>
  </div>
  <div class="body">
    <p>Hi {first},</p>
    <p>Your payment was successful and your plan has been activated.</p>
    <div class="plan-box">
      <div class="plan-name">{plan_name}</div>
      <div style="color:#7d8fa3;margin-top:6px">Active now</div>
    </div>
    <p>Your credits have been topped up and all features are ready to use.</p>
    <div style="text-align:center"><a href="https://ugoingviral.com/app" class="btn">Go to dashboard →</a></div>
  </div>
  <div class="footer">Questions? Email <a href="mailto:support@ugoingviral.com" style="color:#00e5ff">support@ugoingviral.com</a></div>
</div>
</body></html>"""
    return send_system_email(to_addr, subject, html)


def send_payment_failed_email(to_addr: str, name: str) -> bool:
    first = (name or to_addr.split("@")[0]).split(" ")[0].capitalize()
    subject = "Action required: Payment failed for UgoingViral"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{margin:0;padding:0;background:#080c18;font-family:Arial,sans-serif}}
  .wrap{{max-width:560px;margin:40px auto;background:#0d1526;border-radius:16px;border:1px solid rgba(239,68,68,.2);overflow:hidden}}
  .hdr{{background:rgba(239,68,68,.1);padding:28px 36px;text-align:center}}
  .logo{{font-size:20px;font-weight:800;color:#f0f4f8}}.logo span{{color:#00e5ff}}
  .body{{padding:28px 36px;color:#a0b0c0;font-size:14px;line-height:1.6}}
  .warn{{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.25);border-radius:10px;padding:16px;color:#f87171;margin:16px 0}}
  .btn{{display:inline-block;background:#ef4444;color:#fff;padding:12px 32px;border-radius:10px;text-decoration:none;font-weight:700;margin-top:16px}}
  .footer{{padding:16px 36px;font-size:11px;color:#3d4f61;border-top:1px solid rgba(255,255,255,.05);text-align:center}}
</style></head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="logo">Ugoin<span>g</span>Viral</div>
    <div style="font-size:22px;font-weight:800;color:#f87171;margin-top:12px">Payment failed ⚠️</div>
  </div>
  <div class="body">
    <p>Hi {first},</p>
    <div class="warn">We were unable to process your payment. Please update your payment method to keep your plan active.</div>
    <p>If your payment is not resolved, your account will revert to the free plan.</p>
    <div style="text-align:center"><a href="https://ugoingviral.com/app#billing" class="btn">Update payment →</a></div>
  </div>
  <div class="footer">Questions? <a href="mailto:support@ugoingviral.com" style="color:#00e5ff">support@ugoingviral.com</a></div>
</div>
</body></html>"""
    return send_system_email(to_addr, subject, html)


def send_subscription_cancelled_email(to_addr: str, name: str) -> bool:
    first = (name or to_addr.split("@")[0]).split(" ")[0].capitalize()
    subject = "Your UgoingViral subscription has been cancelled"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{margin:0;padding:0;background:#080c18;font-family:Arial,sans-serif}}
  .wrap{{max-width:560px;margin:40px auto;background:#0d1526;border-radius:16px;border:1px solid rgba(255,255,255,.07);overflow:hidden}}
  .hdr{{padding:28px 36px;text-align:center;border-bottom:1px solid rgba(255,255,255,.07)}}
  .logo{{font-size:20px;font-weight:800;color:#f0f4f8}}.logo span{{color:#00e5ff}}
  .body{{padding:28px 36px;color:#a0b0c0;font-size:14px;line-height:1.6}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#00e5ff,#7c3aed);color:#fff;padding:12px 32px;border-radius:10px;text-decoration:none;font-weight:700;margin-top:16px}}
  .footer{{padding:16px 36px;font-size:11px;color:#3d4f61;border-top:1px solid rgba(255,255,255,.05);text-align:center}}
</style></head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="logo">Ugoin<span>g</span>Viral</div>
    <div style="font-size:22px;font-weight:800;color:#f0f4f8;margin-top:12px">Subscription cancelled</div>
  </div>
  <div class="body">
    <p>Hi {first},</p>
    <p>Your subscription has been cancelled. Your account has been moved to the free plan.</p>
    <p>Your content history and account data are still saved. You can reactivate anytime.</p>
    <div style="text-align:center"><a href="https://ugoingviral.com/app#billing" class="btn">Reactivate plan →</a></div>
  </div>
  <div class="footer">Questions? <a href="mailto:support@ugoingviral.com" style="color:#00e5ff">support@ugoingviral.com</a></div>
</div>
</body></html>"""
    return send_system_email(to_addr, subject, html)


def send_reminder_email(to_addr: str, name: str, days_away: int = 3) -> bool:
    first = (name or to_addr.split("@")[0]).split(" ")[0].capitalize()
    subject = f"Hey {first}, your content is waiting 👀"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{margin:0;padding:0;background:#080c18;font-family:Arial,sans-serif}}
  .wrap{{max-width:560px;margin:40px auto;background:#0d1526;border-radius:16px;border:1px solid rgba(255,255,255,.07);overflow:hidden}}
  .hdr{{background:linear-gradient(135deg,rgba(0,229,255,.1),rgba(124,58,237,.1));padding:32px 36px;text-align:center}}
  .logo{{font-size:20px;font-weight:800;color:#f0f4f8}}.logo span{{color:#00e5ff}}
  .body{{padding:28px 36px;color:#a0b0c0;font-size:14px;line-height:1.6}}
  .cta-box{{background:rgba(0,229,255,.06);border:1px solid rgba(0,229,255,.15);border-radius:12px;padding:20px;text-align:center;margin:20px 0}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#00e5ff,#7c3aed);color:#fff;padding:13px 36px;border-radius:10px;text-decoration:none;font-weight:700}}
  .footer{{padding:16px 36px;font-size:11px;color:#3d4f61;border-top:1px solid rgba(255,255,255,.05);text-align:center}}
</style></head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="logo">Ugoin<span>g</span>Viral</div>
    <div style="font-size:24px;font-weight:800;color:#f0f4f8;margin-top:12px">You've been away {days_away} days 👋</div>
  </div>
  <div class="body">
    <p>Hi {first},</p>
    <p>Your social media is running on autopilot — but the more you tune it, the better it performs.</p>
    <div class="cta-box">
      <div style="font-size:14px;color:#7d8fa3;margin-bottom:12px">Your dashboard is ready</div>
      <a href="https://ugoingviral.com/app" class="btn">Continue where you left off →</a>
    </div>
    <p style="font-size:12px;color:#4d5f71">Don't want reminders? You can turn them off in Settings → Notifications.</p>
  </div>
  <div class="footer"><a href="mailto:support@ugoingviral.com" style="color:#00e5ff">support@ugoingviral.com</a></div>
</div>
</body></html>"""
    return send_system_email(to_addr, subject, html)


# ── Auto-svar worker ──────────────────────────────────────────────────────────

@router.post("/api/email/auto-reply/toggle")
async def toggle_auto_reply(req: Request):
    d = await req.json()
    if "email_settings" not in store:
        store["email_settings"] = {}
    store.get("email_settings", {})["auto_reply_active"] = d.get("active", False)
    save_store()
    status = "aktiveret" if d.get("active") else "deaktiveret"
    add_log(f"📧 Auto email svar {status}", "info")
    return {"status": "ok"}


# ── Help & Support contact form ──────────────────────────────────────────────

SUPPORT_INBOX = os.getenv("SUPPORT_INBOX_EMAIL", "support@ugoingviral.com")


@router.post("/api/support/contact")
async def support_contact(req: Request):
    """Forward a Help & Support message to the support inbox and confirm
    receipt to the user. Auth is optional — anonymous users supply their
    email in the form."""
    d = await req.json()
    subject = (d.get("subject") or "").strip() or "Support request"
    message = (d.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    if len(message) > 8000:
        raise HTTPException(status_code=400, detail="Message is too long (max 8000 characters)")

    # Derive the user's identity. Prefer the authenticated session, fall back
    # to whatever they typed in the form.
    user_email = (d.get("email") or "").strip()
    user_name  = (d.get("name") or "").strip()
    user_id    = ""
    try:
        from routes.auth import SECRET_KEY, ALGORITHM
        from jose import jwt as _jwt
        auth = req.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            payload = _jwt.decode(auth[7:], SECRET_KEY, algorithms=[ALGORITHM])
            user_id = payload.get("user_id") or ""
            if user_id:
                from services.users import get_user_by_id
                u = get_user_by_id(user_id) or {}
                user_email = user_email or u.get("email") or ""
                user_name  = user_name  or u.get("name")  or u.get("full_name") or ""
    except Exception:
        pass

    if not user_email:
        raise HTTPException(status_code=400, detail="Email is required")

    safe_msg = (message
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>"))
    safe_name = (user_name or user_email).replace("<", "&lt;").replace(">", "&gt;")
    safe_subject = subject.replace("<", "&lt;").replace(">", "&gt;")

    # 1) Notify the support inbox.
    inbox_html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#f4f5f7;padding:20px">
  <div style="max-width:640px;margin:auto;background:#fff;border-radius:10px;padding:24px;border:1px solid #e5e7eb">
    <h2 style="margin:0 0 8px;color:#111827">New Help &amp; Support message</h2>
    <p style="margin:0 0 16px;color:#6b7280;font-size:13px">via /api/support/contact</p>
    <table style="width:100%;font-size:14px;color:#111827;border-collapse:collapse">
      <tr><td style="padding:4px 0;color:#6b7280;width:90px">From</td><td>{safe_name} &lt;{user_email}&gt;</td></tr>
      <tr><td style="padding:4px 0;color:#6b7280">User ID</td><td>{user_id or '—'}</td></tr>
      <tr><td style="padding:4px 0;color:#6b7280">Subject</td><td><strong>{safe_subject}</strong></td></tr>
    </table>
    <div style="margin-top:18px;padding:14px;background:#f9fafb;border-radius:8px;border:1px solid #e5e7eb;font-size:14px;line-height:1.55;color:#111827;white-space:pre-wrap">{safe_msg}</div>
  </div>
</body></html>"""
    inbox_ok = send_system_email(
        SUPPORT_INBOX,
        f"[Support] {subject} — {user_email}",
        inbox_html,
    )

    # 2) Confirm receipt to the user.
    confirm_html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#f4f5f7;padding:20px">
  <div style="max-width:560px;margin:auto;background:#fff;border-radius:12px;padding:28px;border:1px solid #e5e7eb">
    <h2 style="margin:0 0 10px;color:#111827">We received your message</h2>
    <p style="font-size:14px;color:#374151;line-height:1.6">
      Hi {safe_name or 'there'},<br><br>
      Thanks for reaching out to UgoingViral support — we received your message and
      <strong>we'll reply within 24 hours</strong>.
    </p>
    <div style="margin-top:18px;padding:14px;background:#f9fafb;border-radius:8px;border:1px solid #e5e7eb">
      <div style="font-size:12px;color:#6b7280;margin-bottom:6px">Your message · <em>{safe_subject}</em></div>
      <div style="font-size:13px;color:#111827;line-height:1.55;white-space:pre-wrap">{safe_msg}</div>
    </div>
    <p style="font-size:12px;color:#6b7280;margin-top:18px">If anything needs adding, just reply to this email.</p>
  </div>
</body></html>"""
    send_system_email(user_email, "We received your message — UgoingViral Support", confirm_html)

    add_log(f"📨 Support message from {user_email} forwarded to {SUPPORT_INBOX}", "info")
    return {
        "status":   "ok" if inbox_ok else "queued",
        "message":  "We received your message, we'll reply within 24 hours",
        "to":       SUPPORT_INBOX,
        "inbox_delivered": inbox_ok,
    }
