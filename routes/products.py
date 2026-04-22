from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime
from services.store import store, save_store, add_log
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()

SHOPIFY_CLIENT_ID = "ce8ed6c403b49eba2762320bd7e008ae"
SHOPIFY_CLIENT_SECRET = "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"

async def refresh_shopify_token() -> str:
    s = store.get("settings", {})
    shop = s.get("shopify_store", "").replace("https://","").replace("http://","").strip("/")
    if not shop:
        return ""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"https://{shop}/admin/oauth/access_token",
                data={"grant_type": "client_credentials", "client_id": SHOPIFY_CLIENT_ID, "client_secret": SHOPIFY_CLIENT_SECRET},
                timeout=15
            )
            if r.status_code == 200:
                token = r.json().get("access_token", "")
                if token:
                    store["settings"]["shopify_token"] = token
                    import time
                    store["settings"]["shopify_token_time"] = time.time()
                    save_store()
                    add_log("✅ Shopify token fornyet automatisk", "success")
                    return token
            add_log(f"⚠️ Shopify token fejl: {r.status_code}", "warning")
    except Exception as e:
        add_log(f"⚠️ Shopify token fejl: {str(e)[:80]}", "warning")
    return ""

async def get_shopify_token() -> str:
    import time
    s = store.get("settings", {})
    token = s.get("shopify_token", "")
    token_time = s.get("shopify_token_time", 0)
    if not token or "••••" in token or (time.time() - token_time) > 82800:
        token = await refresh_shopify_token()
    return token

def get_next_image(product_id: str, images: list) -> str:
    if not images: return ""
    if len(images) == 1: return images[0]
    if "image_rotation" not in store: store["image_rotation"] = {}
    current_idx = store["image_rotation"].get(product_id, 0)
    image = images[current_idx % len(images)]
    store["image_rotation"][product_id] = (current_idx + 1) % len(images)
    save_store()
    return image



# ── Products ──────────────────────────────────────────────────────────────────
@router.get("/api/products/manual")
def get_manual():
    prods = store.get("manual_products", [])
    for p in prods:
        p["content_count"] = len(store["product_content"].get(str(p["id"]), []) or store["product_content"].get(p["id"], []))
    return {"products": prods}

@router.post("/api/products/manual")
def add_manual(p: ManualProduct):
    prod = {
        "id": p.id or f"manual_{datetime.now().timestamp()}",
        "title": p.title, "description": p.description,
        "price": p.price, "images": p.images or [],
        "image": p.images[0] if p.images else "",
        "status": "active", "source": "manual", "content_count": 0,
        "group": p.group or "Alle", "created": datetime.now().isoformat(),
    }
    if "manual_products" not in store: store["manual_products"] = []
    existing = [x for x in store["manual_products"] if x["id"] == prod["id"]]
    if existing:
        store["manual_products"] = [prod if x["id"] == prod["id"] else x for x in store["manual_products"]]
    else:
        store["manual_products"].insert(0, prod)
    save_store()
    return {"status": "saved", "product": prod}

@router.get("/api/products/check_discontinued")
async def check_discontinued():
    """Tjek om nogen Shopify-produkter er forsvundet — returner liste til UI."""
    raw_token = await get_shopify_token()
    if not raw_token:
        return {"discontinued": []}
    s = store["settings"]
    store_url = s.get("shopify_store","").replace("https://","").replace("http://","").strip("/")
    if not store_url:
        return {"discontinued": []}
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://{store_url}/admin/api/2024-01/products.json?limit=250",
                headers={"X-Shopify-Access-Token": raw_token}, timeout=15)
            r.raise_for_status()
            live_ids = {str(p["id"]) for p in r.json().get("products", [])}
        cached = store.get("shopify_products_cache", [])
        discontinued = [p for p in cached if p["id"] not in live_ids]
        return {"discontinued": discontinued}
    except:
        return {"discontinued": []}

@router.post("/api/products/archive/{pid}")
def archive_product(pid: str):
    """Flyt produkt til udgået-liste i stedet for at slette."""
    all_prods = store.get("manual_products", []) + store.get("shopify_products_cache", [])
    prod = next((p for p in all_prods if p["id"] == pid), None)
    if not prod:
        return {"status": "not_found"}
    prod["status"] = "archived"
    prod["archived_at"] = datetime.now().isoformat()
    if "archived_products" not in store:
        store["archived_products"] = []
    if not any(p["id"] == pid for p in store["archived_products"]):
        store["archived_products"].append(prod)
    store["shopify_products_cache"] = [p for p in store.get("shopify_products_cache",[]) if p["id"] != pid]
    save_store()
    return {"status": "archived"}

@router.delete("/api/products/manual/{pid}")
def delete_manual(pid: str):
    store["manual_products"] = [p for p in store.get("manual_products", []) if p["id"] != pid]
    save_store()
    return {"status": "deleted"}

@router.get("/api/products/groups")
def get_groups():
    prods = store.get("manual_products", [])
    # Groups from products
    prod_groups = set([p.get("group", "") for p in prods if p.get("group") and p.get("group") != "Alle"])
    # Saved groups
    saved_groups = set(store.get("groups", []))
    all_groups = prod_groups | saved_groups
    groups = ["Alle"] + sorted(list(all_groups))
    return {"groups": groups}

@router.post("/api/products/groups/{group_name}")
def create_group(group_name: str):
    if "groups" not in store:
        store["groups"] = []
    if group_name not in store["groups"]:
        store["groups"].append(group_name)
        save_store()
    return {"status": "ok", "group": group_name}

@router.delete("/api/products/groups/{group_name}")
def delete_group(group_name: str):
    if "groups" in store and group_name in store["groups"]:
        store["groups"].remove(group_name)
        save_store()
    return {"status": "ok"}

@router.get("/api/shopify/products")
async def get_products():
    s = store["settings"]
    manual = store.get("manual_products", [])
    for p in manual:
        p["content_count"] = len(store["product_content"].get(str(p["id"]), []) or store["product_content"].get(p["id"], []))
    if not s.get("shopify_store"):
        return {"products": manual if manual else _demo_products(), "source": "manual" if manual else "demo"}
    # Hent gyldig token — fornyer automatisk hvis udløbet
    raw_token = await get_shopify_token()
    if not raw_token:
        return {"products": manual if manual else _demo_products(), "source": "manual" if manual else "demo", "shopify_error": "Token kunne ikke hentes"}
    store_url = s["shopify_store"].replace("https://", "").replace("http://", "").strip("/")
    async with httpx.AsyncClient() as c:
        try:
            r = await c.get(f"https://{store_url}/admin/api/2024-01/products.json?limit=50",
                headers={"X-Shopify-Access-Token": raw_token}, timeout=15)
            if r.status_code == 401:
                # Token udløbet — forsøg refresh
                raw_token = await refresh_shopify_token()
                if raw_token:
                    r = await c.get(f"https://{store_url}/admin/api/2024-01/products.json?limit=50",
                        headers={"X-Shopify-Access-Token": raw_token}, timeout=15)
            r.raise_for_status()
            products = []
            for p in r.json().get("products", []):
                imgs = p.get("images", [])
                products.append({
                    "id": str(p["id"]), "title": p["title"],
                    "description": p.get("body_html", "").replace("<p>", "").replace("</p>", "").strip()[:300],
                    "price": p.get("variants", [{}])[0].get("price", "0"),
                    "images": [i.get("src", "") for i in imgs],
                    "image": imgs[0].get("src", "") if imgs else "",
                    "status": p.get("status", "active"), "source": "shopify",
                    "group": "Alle",
                    "content_count": len(store["product_content"].get(str(p["id"]), [])),
                })
            return {"products": manual + products, "source": "shopify"}
        except Exception:
            cached = store.get("shopify_products_cache", [])
            for p in cached:
                p["content_count"] = len(store["product_content"].get(str(p["id"]), []))
            return {"products": manual + cached, "source": "cache"}

def _demo_products():
    data = [("demo1","Premium T-Shirt","299"), ("demo2","Sneakers Sort","599"),
            ("demo3","Hoodie Grå","449"), ("demo4","Cap Navy","199")]
    return [{"id": i, "title": t, "description": f"Flot {t.lower()}", "price": p,
             "images": [], "image": "", "status": "active", "source": "demo",
             "group": "Alle", "content_count": len(store["product_content"].get(i, []))} for i, t, p in data]

@router.get("/api/products/{pid}/content")
def get_product_content(pid: str):
    # Prøv begge ID formater — Shopify bruger integer, manuelle bruger string
    content_list = (
        store["product_content"].get(str(pid)) or
        store["product_content"].get(pid) or
        []
    )
    return {"content": content_list}

@router.delete("/api/products/{pid}/content/{cid}")
def del_product_content(pid: str, cid: str):
    store["product_content"][pid] = [i for i in store["product_content"].get(pid, []) if i["id"] != cid]
    save_store()
    return {"status": "deleted"}
