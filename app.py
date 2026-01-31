from flask import Flask, render_template, request, redirect, url_for, session
import json
import os
from datetime import datetime
import random
import string

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change_this_secret_key_now")

SHIPMENTS_FILE = "shipments.json"
USERS_FILE = "users.json"
APPLICATIONS_FILE = "applications.json"
CHATS_FILE = "chats.json"   # chat storage

DEFAULT_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@glopacshippingexpress.com").strip().lower()
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


# -----------------------
# JSON helpers (safe)
# -----------------------
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return default
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


# -----------------------
# Time helpers
# -----------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def now_ts():
    return int(datetime.utcnow().timestamp())


def parse_dt(dt_str: str):
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    except Exception:
        return None


def sort_events(events):
    if not isinstance(events, list):
        return []

    def key_fn(e):
        d = parse_dt((e or {}).get("date", ""))
        return d if d else datetime.max

    return sorted(events, key=key_fn)


# -----------------------
# Users / Auth helpers (PLAIN PASSWORDS)
# -----------------------
def ensure_default_admin_exists():
    users = load_json(USERS_FILE, {})
    if DEFAULT_ADMIN_EMAIL not in users:
        users[DEFAULT_ADMIN_EMAIL] = {
            "email": DEFAULT_ADMIN_EMAIL,
            "name": "Glopac Shipping Express Admin",
            "password": DEFAULT_ADMIN_PASSWORD,  # PLAIN (demo)
            "role": "admin",
            "active": True,
            "created_at": now_str(),
        }
        save_json(USERS_FILE, users)


def current_user():
    email = session.get("user_email")
    if not email:
        return None
    email = email.strip().lower()

    users = load_json(USERS_FILE, {})
    u = users.get(email)
    if not u or not u.get("active", True):
        return None

    return {
        "email": u.get("email", email),
        "name": u.get("name", ""),
        "role": u.get("role", "user"),
        "active": u.get("active", True),
    }


def is_logged_in():
    return current_user() is not None


def is_admin():
    u = current_user()
    return bool(u and u.get("role") == "admin")


def require_login(next_url=None):
    if not is_logged_in():
        if next_url:
            return redirect(url_for("login", next=next_url))
        return redirect(url_for("login"))
    return None


def require_admin(next_url=None):
    ensure_default_admin_exists()
    if not is_admin():
        if next_url:
            return redirect(url_for("login", next=next_url))
        return redirect(url_for("login"))
    return None


# -----------------------
# Fees helpers
# -----------------------
def _to_float(v):
    try:
        if v is None:
            return None
        s = str(v).strip()
        if s == "":
            return None
        return float(s)
    except Exception:
        return None


def apply_fees_logic(shipment: dict, status: str, fees_amount_raw, fees_reason_raw, clear_fees: bool):
    if clear_fees:
        shipment["fees"] = None
        return

    fees_amount = _to_float(fees_amount_raw)
    fees_reason = (fees_reason_raw or "").strip() or "Customs and Taxes"

    if status == "On Hold" and fees_amount is not None:
        fees = shipment.get("fees")
        if not isinstance(fees, dict):
            fees = {}
        fees["amount"] = fees_amount
        fees["reason"] = fees_reason
        fees["paid"] = False
        fees.setdefault("payment_submitted", False)
        shipment["fees"] = fees


def add_status_event_if_changed(shipment: dict, old_status: str, new_status: str):
    if (old_status or "") != (new_status or ""):
        shipment.setdefault("events", []).append({
            "date": now_str(),
            "location": "Admin Update",
            "description": f"Status updated: {old_status or 'N/A'} → {new_status}"
        })


# -----------------------
# ✅ AUTO SHIPMENT HISTORY (milestones)
# -----------------------
def _event_key_list(shipment: dict):
    shipment.setdefault("_auto_event_keys", [])
    if not isinstance(shipment["_auto_event_keys"], list):
        shipment["_auto_event_keys"] = []
    return shipment["_auto_event_keys"]


def add_auto_event_once(shipment: dict, key: str, location: str, description: str) -> bool:
    if not isinstance(shipment, dict) or not key:
        return False

    keys = _event_key_list(shipment)
    if key in keys:
        return False

    shipment.setdefault("events", [])
    if not isinstance(shipment["events"], list):
        shipment["events"] = []

    shipment["events"].append({
        "date": now_str(),
        "location": location,
        "description": description
    })
    keys.append(key)
    return True


def normalize_status_bucket(status: str) -> str:
    s = (status or "").strip().lower()
    if "delivered" in s:
        return "delivered"
    if "out for delivery" in s:
        return "out_for_delivery"
    if "transit" in s:
        return "in_transit"
    if "picked up" in s:
        return "picked_up"
    if "hold" in s or "pending verification" in s:
        return "on_hold"
    if "created" in s:
        return "created"
    return "other"


def ensure_auto_history(shipment: dict) -> bool:
    changed = False
    if not isinstance(shipment, dict):
        return False

    status = shipment.get("status", "Unknown")
    bucket = normalize_status_bucket(status)

    changed |= add_auto_event_once(
        shipment,
        "milestone_created",
        "System",
        "Shipment record created"
    )

    if bucket == "picked_up":
        changed |= add_auto_event_once(shipment, "milestone_picked_up", "Carrier Scan", "Shipment picked up")
    elif bucket == "in_transit":
        changed |= add_auto_event_once(shipment, "milestone_in_transit", "Transit Hub", "Shipment is in transit")
    elif bucket == "out_for_delivery":
        changed |= add_auto_event_once(shipment, "milestone_out_for_delivery", "Destination Facility", "Out for delivery")
    elif bucket == "delivered":
        changed |= add_auto_event_once(shipment, "milestone_delivered", "Delivered", "Shipment delivered successfully")
    elif bucket == "on_hold":
        changed |= add_auto_event_once(shipment, "milestone_on_hold", "Customs / Compliance", "Shipment placed on hold")

    return changed


def add_estimated_delivery_event_if_changed(shipment: dict, old_est: str, new_est: str) -> bool:
    old_est = (old_est or "").strip()
    new_est = (new_est or "").strip()

    if old_est == new_est:
        return False

    key = f"estimated_delivery:{new_est or 'cleared'}"
    if new_est:
        return add_auto_event_once(shipment, key, "Admin Update", f"Estimated delivery set: {new_est}")
    else:
        return add_auto_event_once(shipment, key, "Admin Update", "Estimated delivery cleared")


# -----------------------
# Route auto-generation
# (North America, South America, Asia, Europe ONLY)
# -----------------------
CITY_DB = {
    "new york": {"lat": 40.7128, "lng": -74.0060, "label": "New York, USA"},
    "los angeles": {"lat": 34.0522, "lng": -118.2437, "label": "Los Angeles, USA"},
    "miami": {"lat": 25.7617, "lng": -80.1918, "label": "Miami, USA"},
    "houston": {"lat": 29.7604, "lng": -95.3698, "label": "Houston, USA"},
    "chicago": {"lat": 41.8781, "lng": -87.6298, "label": "Chicago, USA"},
    "toronto": {"lat": 43.6532, "lng": -79.3832, "label": "Toronto, Canada"},
    "vancouver": {"lat": 49.2827, "lng": -123.1207, "label": "Vancouver, Canada"},
    "mexico city": {"lat": 19.4326, "lng": -99.1332, "label": "Mexico City, Mexico"},

    "sao paulo": {"lat": -23.5505, "lng": -46.6333, "label": "São Paulo, Brazil"},
    "rio de janeiro": {"lat": -22.9068, "lng": -43.1729, "label": "Rio de Janeiro, Brazil"},
    "buenos aires": {"lat": -34.6037, "lng": -58.3816, "label": "Buenos Aires, Argentina"},
    "bogota": {"lat": 4.7110, "lng": -74.0721, "label": "Bogotá, Colombia"},
    "lima": {"lat": -12.0464, "lng": -77.0428, "label": "Lima, Peru"},
    "santiago": {"lat": -33.4489, "lng": -70.6693, "label": "Santiago, Chile"},

    "london": {"lat": 51.5074, "lng": -0.1278, "label": "London, UK"},
    "paris": {"lat": 48.8566, "lng": 2.3522, "label": "Paris, France"},
    "madrid": {"lat": 40.4168, "lng": -3.7038, "label": "Madrid, Spain"},
    "rome": {"lat": 41.9028, "lng": 12.4964, "label": "Rome, Italy"},
    "berlin": {"lat": 52.5200, "lng": 13.4050, "label": "Berlin, Germany"},
    "amsterdam": {"lat": 52.3676, "lng": 4.9041, "label": "Amsterdam, Netherlands"},

    "dubai": {"lat": 25.2048, "lng": 55.2708, "label": "Dubai, UAE"},
    "tokyo": {"lat": 35.6762, "lng": 139.6503, "label": "Tokyo, Japan"},
    "seoul": {"lat": 37.5665, "lng": 126.9780, "label": "Seoul, South Korea"},
    "singapore": {"lat": 1.3521, "lng": 103.8198, "label": "Singapore"},
    "hong kong": {"lat": 22.3193, "lng": 114.1694, "label": "Hong Kong"},
    "delhi": {"lat": 28.6139, "lng": 77.2090, "label": "Delhi, India"},
}


def find_city_coords(text: str):
    if not text:
        return None
    t = text.strip().lower()
    for k, v in CITY_DB.items():
        if k in t:
            return v
    return None


def lerp(a, b, t):
    return a + (b - a) * t


def generate_route(origin_text: str, dest_text: str):
    o = find_city_coords(origin_text)
    d = find_city_coords(dest_text)

    if not o:
        o = {"lat": 40.7128, "lng": -74.0060, "label": origin_text or "New York, USA"}
    if not d:
        d = {"lat": o["lat"] + 10.0, "lng": o["lng"] + 10.0, "label": dest_text or "Destination"}

    labels = [
        f"Origin Scan — {o['label']}",
        "Departed origin facility",
        "In transit — regional hub",
        "Arrived in destination country",
        f"Destination Facility — {d['label']}",
    ]
    steps = [0.0, 0.22, 0.55, 0.82, 1.0]

    points = []
    for lab, t in zip(labels, steps):
        points.append({
            "lat": round(lerp(o["lat"], d["lat"], t), 6),
            "lng": round(lerp(o["lng"], d["lng"], t), 6),
            "label": lab
        })
    return points


def should_regenerate_route(existing: dict, updated: dict) -> bool:
    old_o = (existing.get("origin") or "").strip()
    old_d = (existing.get("destination") or "").strip()
    new_o = (updated.get("origin") or "").strip()
    new_d = (updated.get("destination") or "").strip()

    route = updated.get("route")
    route_empty = (not isinstance(route, list)) or (len(route) == 0)
    changed_od = (old_o != new_o) or (old_d != new_d)

    return (new_o != "" and new_d != "") and (route_empty or changed_od)


# -----------------------
# CHAT helpers
# -----------------------
def _chat_safe_tracking(tracking_id: str):
    return (tracking_id or "").strip()


def _make_code(n=8):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def chat_get_thread(tracking_id: str):
    chats = load_json(CHATS_FILE, {})
    return chats.get(tracking_id)


def chat_ensure_thread(tracking_id: str, owner_email: str):
    tracking_id = _chat_safe_tracking(tracking_id)
    chats = load_json(CHATS_FILE, {})
    th = chats.get(tracking_id)
    if not isinstance(th, dict):
        th = {"tracking_id": tracking_id, "owner_email": owner_email, "messages": []}
        chats[tracking_id] = th
        save_json(CHATS_FILE, chats)
    else:
        th.setdefault("messages", [])
        if owner_email and not th.get("owner_email"):
            th["owner_email"] = owner_email
            chats[tracking_id] = th
            save_json(CHATS_FILE, chats)
    return th


def chat_add_message(tracking_id: str, sender: str, text: str):
    tracking_id = _chat_safe_tracking(tracking_id)
    text = (text or "").strip()
    if not text:
        return

    chats = load_json(CHATS_FILE, {})
    th = chats.get(tracking_id)
    if not isinstance(th, dict):
        th = {"tracking_id": tracking_id, "owner_email": "", "messages": []}

    th.setdefault("messages", [])
    th["messages"].append({
        "ts": now_ts(),
        "time": now_str(),
        "sender": sender,     # "user" | "admin" | "system"
        "text": text
    })

    chats[tracking_id] = th
    save_json(CHATS_FILE, chats)


def chat_mark_read(tracking_id: str):
    tracking_id = _chat_safe_tracking(tracking_id)
    last_read = session.get("chat_last_read", {})
    if not isinstance(last_read, dict):
        last_read = {}
    last_read[tracking_id] = now_ts()
    session["chat_last_read"] = last_read


def chat_unread_count_for_user(user_email: str):
    user_email = (user_email or "").strip().lower()
    if not user_email:
        return (0, None)

    chats = load_json(CHATS_FILE, {})
    last_read = session.get("chat_last_read", {})
    if not isinstance(last_read, dict):
        last_read = {}

    unread = 0
    latest_tid = None
    latest_ts = -1

    for tid, th in chats.items():
        if not isinstance(th, dict):
            continue
        owner = (th.get("owner_email") or "").strip().lower()
        if owner != user_email:
            continue

        lr = int(last_read.get(tid, 0) or 0)
        msgs = th.get("messages") or []
        for m in msgs:
            if (m.get("sender") in ("admin", "system")) and int(m.get("ts", 0)) > lr:
                unread += 1
                if int(m.get("ts", 0)) > latest_ts:
                    latest_ts = int(m.get("ts", 0))
                    latest_tid = tid

    return (unread, latest_tid)


@app.context_processor
def inject_chat_notifications():
    u = current_user()
    if not u or u.get("role") == "admin":
        return {"chat_unread_count": 0, "chat_latest_tracking_id": None}
    c, tid = chat_unread_count_for_user(u.get("email"))
    return {"chat_unread_count": c, "chat_latest_tracking_id": tid}


# -----------------------
# Home
# -----------------------
@app.route("/")
def index():
    return render_template("index.html", user=current_user())


# -----------------------
# Login / Logout
# -----------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    ensure_default_admin_exists()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        users = load_json(USERS_FILE, {})
        u = users.get(email)

        if (not u) or (not u.get("active", True)) or (u.get("password") != password):
            return render_template("login.html", user=current_user(), error="Invalid email or password"), 401

        session["user_email"] = email

        nxt = (request.form.get("next") or request.args.get("next") or "").strip()
        if nxt and not nxt.startswith("/"):
            nxt = ""

        if u.get("role") == "admin":
            return redirect(url_for("admin_panel"))

        if nxt:
            return redirect(nxt)

        return redirect(url_for("my_shipments"))

    return render_template("login.html", user=current_user())


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    return redirect(url_for("index"))


# -----------------------
# Sign up (APPLICATION ONLY)
# -----------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        reason = request.form.get("reason", "").strip()

        if not name or not email:
            return render_template("signup.html", user=current_user(),
                                   error="Name and email are required."), 400

        users = load_json(USERS_FILE, {})
        if email in users:
            return render_template("signup.html", user=current_user(),
                                   error="This email already has an account. Please log in."), 400

        applications = load_json(APPLICATIONS_FILE, {})
        existing = applications.get(email)
        if existing and existing.get("status") == "pending":
            return render_template("signup.html", user=current_user(),
                                   error="Your application is already pending. Please wait for approval."), 400

        applications[email] = {
            "name": name,
            "email": email,
            "reason": reason or "",
            "status": "pending",
            "submitted_at": now_str(),
            "admin_note": ""
        }
        save_json(APPLICATIONS_FILE, applications)

        return render_template("signup.html", user=current_user(),
                               success="Account creating pending approval. You will be contacted by email after review.")

    return render_template("signup.html", user=current_user())


# -----------------------
# Ship page (UI-only)
# -----------------------
@app.route("/ship", methods=["GET", "POST"])
def ship():
    if request.method == "POST":
        return render_template("ship.html", user=current_user(),
                               success="✅ Shipment request received. We’ll contact you by email with next steps.")
    return render_template("ship.html", user=current_user())


# -----------------------
# ✅ Support Chat launcher (THIS FIXES /support_chat NOT FOUND)
# -----------------------
@app.route("/support_chat")
def support_chat():
    gate = require_login(next_url="/support_chat")
    if gate:
        return gate

    u = current_user()
    viewer_email = (u.get("email") or "").strip().lower()

    shipments = load_json(SHIPMENTS_FILE, {})

    # find newest shipment for this user (by last event time)
    latest_tid = None
    latest_time = ""

    for tid, s in shipments.items():
        if not isinstance(s, dict):
            continue
        owner = (s.get("owner_email") or "").strip().lower()
        if owner != viewer_email:
            continue

        # try use last event date as "most recent"
        evs = s.get("events") or []
        t = ""
        if isinstance(evs, list) and evs:
            last = evs[-1]
            if isinstance(last, dict):
                t = last.get("date", "") or ""

        if t >= latest_time:
            latest_time = t
            latest_tid = tid

    if not latest_tid:
        return render_template("support.html", user=u, error="No shipment found for your account yet.")

    return redirect(url_for("payment_chat", tracking_id=latest_tid))


# -----------------------
# My Shipments
# -----------------------
@app.route("/my_shipments")
def my_shipments():
    gate = require_login(next_url=url_for("my_shipments"))
    if gate:
        return gate

    u = current_user()
    viewer_email = (u.get("email") or "").strip().lower()

    shipments = load_json(SHIPMENTS_FILE, {})
    my_list = []
    for tid, s in shipments.items():
        owner = (s.get("owner_email") or "").strip().lower()
        if owner == viewer_email:
            my_list.append({
                "tracking_id": tid,
                "status": s.get("status", "Unknown"),
                "estimated_delivery": s.get("estimated_delivery"),
                "estimated_delivery_tbd_on_hold": bool(s.get("estimated_delivery_tbd_on_hold", False)),
                "origin": s.get("origin"),
                "destination": s.get("destination"),
                "fees": s.get("fees"),
            })

    my_list.sort(key=lambda x: (x.get("estimated_delivery") or ""), reverse=True)
    return render_template("my_shipments.html", user=u, shipments=my_list)


@app.route("/my-shipments")
def my_shipments_alias():
    return redirect(url_for("my_shipments"))


# -----------------------
# Tracking
# -----------------------
@app.route("/track", methods=["GET", "POST"])
def track():
    tracking_id = (
        request.form.get("tracking_id", "").strip()
        if request.method == "POST"
        else request.args.get("tracking_id", "").strip()
    )

    if not tracking_id:
        return render_template("index.html", error="Enter a tracking ID", user=current_user())

    shipments = load_json(SHIPMENTS_FILE, {})
    shipment = shipments.get(tracking_id)
    if not shipment:
        return render_template("index.html", error="Tracking ID not found", user=current_user())

    changed = ensure_auto_history(shipment)

    route = shipment.get("route") or []
    if not isinstance(route, list):
        route = []

    if not shipment.get("current_location") and route:
        last = route[-1]
        if isinstance(last, dict) and "lat" in last and "lng" in last:
            shipment["current_location"] = {"lat": last["lat"], "lng": last["lng"]}
            changed = True

    if changed:
        shipments[tracking_id] = shipment
        save_json(SHIPMENTS_FILE, shipments)

    current_location = shipment.get("current_location")
    events = sort_events(shipment.get("events", []))

    u = current_user()
    logged = is_logged_in()
    admin = is_admin()

    owner_email = (shipment.get("owner_email") or "").strip().lower()
    viewer_email = (u.get("email") if u else "").strip().lower()
    is_owner = logged and owner_email and (viewer_email == owner_email)

    fees = shipment.get("fees")
    display_status = shipment.get("status", "Unknown")
    can_view_sensitive = admin or is_owner

    fees_visible = None
    if fees and isinstance(fees, dict) and not fees.get("paid", True):
        if not can_view_sensitive:
            display_status = "Package held for customs fees — log in to continue and take further action"
            fees_visible = {"exists": True, "paid": False}
        else:
            if fees.get("payment_submitted"):
                display_status = "Payment Submitted - Pending Verification"
            else:
                display_status = "On Hold for Customs/Taxes Payment"
            fees_visible = fees
    else:
        fees_visible = fees if can_view_sensitive else None

    est = shipment.get("estimated_delivery")
    est_tbd = bool(shipment.get("estimated_delivery_tbd_on_hold", False))
    if est_tbd:
        est = "Date will be made available when hold clears"

    return render_template(
        "track.html",
        user=u,
        logged_in=logged,
        is_admin=admin,
        is_owner=is_owner,
        owner_email=owner_email,
        tracking_id=tracking_id,
        status=display_status,
        estimated_delivery=est,
        origin=shipment.get("origin"),
        destination=shipment.get("destination"),
        package_details=shipment.get("package_details"),
        route=route,
        current_location=current_location,
        events=events,
        fees=fees_visible,
    )


# -----------------------
# Payment page
# -----------------------
@app.route("/pay/<tracking_id>", methods=["GET"])
def payment_page(tracking_id):
    gate = require_login(next_url=url_for("payment_page", tracking_id=tracking_id))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    shipment = shipments.get(tracking_id)
    if not shipment:
        return redirect(url_for("track", tracking_id=tracking_id))

    u = current_user()
    admin = is_admin()
    owner_email = (shipment.get("owner_email") or "").strip().lower()
    viewer_email = (u.get("email") if u else "").strip().lower()
    is_owner = owner_email and (viewer_email == owner_email)

    if not (admin or is_owner):
        return redirect(url_for("track", tracking_id=tracking_id))

    fees = shipment.get("fees")
    if not fees or not isinstance(fees, dict) or fees.get("paid"):
        return redirect(url_for("track", tracking_id=tracking_id))

    return render_template("payment.html", user=u, tracking_id=tracking_id, fees=fees)


# -----------------------
# Initiate Payment -> generates code and opens chat
# -----------------------
@app.route("/initiate_payment/<tracking_id>", methods=["POST"])
def initiate_payment(tracking_id):
    gate = require_login(next_url=url_for("payment_page", tracking_id=tracking_id))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    shipment = shipments.get(tracking_id)
    if not shipment:
        return redirect(url_for("track", tracking_id=tracking_id))

    u = current_user()
    admin = is_admin()

    owner_email = (shipment.get("owner_email") or "").strip().lower()
    viewer_email = (u.get("email") if u else "").strip().lower()
    is_owner = owner_email and (viewer_email == owner_email)
    if not (admin or is_owner):
        return redirect(url_for("track", tracking_id=tracking_id))

    fees = shipment.get("fees")
    if not fees or not isinstance(fees, dict) or fees.get("paid"):
        return redirect(url_for("track", tracking_id=tracking_id))

    payment_method = (request.form.get("payment_method") or "").strip()
    payer_email = (request.form.get("payer_email") or "").strip().lower()

    if not payment_method or not payer_email:
        return redirect(url_for("payment_page", tracking_id=tracking_id))

    code = _make_code(8)

    fees["payment_method"] = payment_method
    fees["payer_email"] = payer_email
    fees["init_code"] = code
    fees["init_code_received"] = False
    fees["payment_submitted"] = False
    fees["initiated_at"] = now_str()

    shipment["fees"] = fees
    shipments[tracking_id] = shipment
    save_json(SHIPMENTS_FILE, shipments)

    chat_ensure_thread(tracking_id, owner_email)

    # ✅ your auto-message (you can change the wording here anytime)
    chat_add_message(
        tracking_id,
        "system",
        f"Payment initiated. Your verification code is: {code}. A representative will be in touch with you shortly. Please send this code in this chat to continue."
    )

    return redirect(url_for("payment_chat", tracking_id=tracking_id))


# -----------------------
# Payment chat (User)
# -----------------------
@app.route("/payment_chat/<tracking_id>", methods=["GET", "POST"])
def payment_chat(tracking_id):
    gate = require_login(next_url=url_for("payment_chat", tracking_id=tracking_id))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    shipment = shipments.get(tracking_id)
    if not shipment:
        return redirect(url_for("track", tracking_id=tracking_id))

    u = current_user()
    admin = is_admin()

    owner_email = (shipment.get("owner_email") or "").strip().lower()
    viewer_email = (u.get("email") if u else "").strip().lower()
    is_owner = owner_email and (viewer_email == owner_email)
    if not (admin or is_owner):
        return redirect(url_for("track", tracking_id=tracking_id))

    chat_mark_read(tracking_id)

    fees = shipment.get("fees") if isinstance(shipment.get("fees"), dict) else {}
    thread = chat_ensure_thread(tracking_id, owner_email)
    msgs = thread.get("messages") or []

    if request.method == "POST":
        text = (request.form.get("message") or "").strip()
        if text:
            chat_add_message(tracking_id, "user", text)

            init_code = (fees.get("init_code") or "").strip().upper()
            if init_code and (not fees.get("init_code_received")):
                if init_code in text.upper():
                    fees["init_code_received"] = True
                    fees["payment_submitted"] = True
                    shipment["fees"] = fees
                    shipment["status"] = "On Hold"

                    shipment.setdefault("events", []).append({
                        "date": now_str(),
                        "location": "Payment Chat",
                        "description": "Verification code received - awaiting payment details"
                    })

                    ensure_auto_history(shipment)

                    shipments[tracking_id] = shipment
                    save_json(SHIPMENTS_FILE, shipments)

                    chat_add_message(
                        tracking_id,
                        "system",
                        "✅ Code received. A representative will send payment details shortly."
                    )

        return redirect(url_for("payment_chat", tracking_id=tracking_id))

    return render_template(
        "payment_chat.html",
        user=u,
        tracking_id=tracking_id,
        messages=msgs
    )


# -----------------------
# Admin chat (Admin)
# -----------------------
@app.route("/admin/chat/<tracking_id>", methods=["GET", "POST"])
def admin_chat(tracking_id):
    gate = require_admin(next_url=url_for("admin_chat", tracking_id=tracking_id))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    shipment = shipments.get(tracking_id, {})
    owner_email = (shipment.get("owner_email") or "").strip().lower()

    thread = chat_ensure_thread(tracking_id, owner_email)
    msgs = thread.get("messages") or []

    if request.method == "POST":
        text = (request.form.get("message") or "").strip()
        if text:
            chat_add_message(tracking_id, "admin", text)
        return redirect(url_for("admin_chat", tracking_id=tracking_id))

    return render_template(
        "admin_chat.html",
        user=current_user(),
        tracking_id=tracking_id,
        owner_email=owner_email,
        messages=msgs
    )


# -----------------------
# Admin Panel (Shipments + Applications)
# -----------------------
@app.route("/admin", methods=["GET", "POST"])
def admin_panel():
    gate = require_admin(next_url=url_for("admin_panel"))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    applications = load_json(APPLICATIONS_FILE, {})

    if request.method == "POST":
        form_type = (request.form.get("form_type") or "").strip().lower()
        if form_type != "shipment":
            return redirect(url_for("admin_panel"))

        tracking_id = request.form.get("tracking_id", "").strip()
        status = request.form.get("status", "").strip()
        owner_email = request.form.get("owner_email", "").strip().lower()

        origin = request.form.get("origin", "").strip()
        destination = request.form.get("destination", "").strip()
        package_details = request.form.get("package_details", "").strip()

        old_est = (shipments.get(tracking_id, {}) or {}).get("estimated_delivery")
        estimated_delivery = request.form.get("estimated_delivery", "").strip()
        estimated_delivery_tbd = True if request.form.get("estimated_delivery_tbd_on_hold") == "on" else False

        fees_amount_raw = request.form.get("fees_amount")
        fees_reason_raw = request.form.get("fees_reason")
        clear_fees = False
        fees_paid = True if request.form.get("fees_paid") == "on" else False

        route_raw = (request.form.get("route") or "").strip()
        current_location_raw = (request.form.get("current_location") or "").strip()

        if not tracking_id or not status:
            return ("Missing required fields (tracking_id, status).", 400)

        existing = shipments.get(tracking_id, {})
        old_status = existing.get("status", "")

        updated = {**existing}
        updated["status"] = status

        if owner_email:
            updated["owner_email"] = owner_email
        else:
            updated.setdefault("owner_email", existing.get("owner_email", ""))

        if origin:
            updated["origin"] = origin
        if destination:
            updated["destination"] = destination
        if package_details:
            updated["package_details"] = package_details

        updated["estimated_delivery_tbd_on_hold"] = bool(estimated_delivery_tbd)
        if estimated_delivery_tbd:
            updated["estimated_delivery"] = None
        else:
            if estimated_delivery:
                updated["estimated_delivery"] = estimated_delivery

        # route
        if route_raw:
            try:
                route = json.loads(route_raw)
                if not isinstance(route, list):
                    return ("Route must be a JSON array", 400)
                updated["route"] = route
            except Exception:
                return ("Invalid JSON for route", 400)
        else:
            updated.setdefault("route", existing.get("route") if isinstance(existing.get("route"), list) else [])
            if should_regenerate_route(existing, updated):
                updated["route"] = generate_route(updated.get("origin"), updated.get("destination"))

        # current location
        if current_location_raw:
            try:
                cl = json.loads(current_location_raw)
                if not isinstance(cl, dict):
                    return ("Current location must be a JSON object", 400)
                updated["current_location"] = cl
            except Exception:
                return ("Invalid JSON for current_location", 400)
        else:
            updated.setdefault("current_location", existing.get("current_location"))

        updated.setdefault("events", existing.get("events", []))

        apply_fees_logic(updated, updated["status"], fees_amount_raw, fees_reason_raw, clear_fees)

        if fees_paid and isinstance(updated.get("fees"), dict):
            updated["fees"]["paid"] = True
            updated["fees"]["payment_submitted"] = False

        add_status_event_if_changed(updated, old_status, updated["status"])

        new_est = updated.get("estimated_delivery") or ""
        add_estimated_delivery_event_if_changed(updated, old_est or "", new_est)

        ensure_auto_history(updated)

        shipments[tracking_id] = updated
        save_json(SHIPMENTS_FILE, shipments)
        return redirect(url_for("admin_panel"))

    ship_list = []
    for tid, s in shipments.items():
        ship_list.append({
            "tracking_id": tid,
            "status": s.get("status", "Unknown"),
            "fees": s.get("fees"),
            "owner_email": s.get("owner_email", ""),
            "origin": s.get("origin"),
            "destination": s.get("destination"),
            "estimated_delivery": s.get("estimated_delivery"),
            "estimated_delivery_tbd_on_hold": bool(s.get("estimated_delivery_tbd_on_hold", False)),
        })

    app_list = []
    for email, a in applications.items():
        if isinstance(a, dict):
            app_list.append({
                "email": a.get("email", email),
                "name": a.get("name", ""),
                "reason": a.get("reason", ""),
                "status": a.get("status", "pending"),
                "submitted_at": a.get("submitted_at", ""),
                "admin_note": a.get("admin_note", ""),
            })
    app_list.sort(key=lambda x: x.get("submitted_at", ""), reverse=True)

    return render_template("admin.html", user=current_user(), shipments=ship_list, applications=app_list)


# -----------------------
# Admin inline update route
# -----------------------
@app.route("/admin/update/<tracking_id>", methods=["POST"])
def admin_update_shipment(tracking_id):
    gate = require_admin(next_url=url_for("admin_panel"))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    shipment = shipments.get(tracking_id)
    if not shipment:
        return redirect(url_for("admin_panel"))

    old_status = shipment.get("status", "")
    old_est = shipment.get("estimated_delivery")

    status = (request.form.get("status") or "").strip()
    custom_status = (request.form.get("custom_status") or "").strip()
    if status == "Custom Status" and custom_status:
        status = custom_status

    fees_amount_raw = request.form.get("fees_amount")
    fees_reason_raw = request.form.get("fees_reason")
    clear_fees = request.form.get("clear_fees") == "1"

    estimated_delivery = (request.form.get("estimated_delivery") or "").strip()
    estimated_delivery_tbd = True if request.form.get("estimated_delivery_tbd_on_hold") == "on" else False

    origin = (request.form.get("origin") or "").strip()
    destination = (request.form.get("destination") or "").strip()

    if origin:
        shipment["origin"] = origin
    if destination:
        shipment["destination"] = destination

    if status:
        shipment["status"] = status

    shipment["estimated_delivery_tbd_on_hold"] = bool(estimated_delivery_tbd)
    if estimated_delivery_tbd:
        shipment["estimated_delivery"] = None
    else:
        if estimated_delivery:
            shipment["estimated_delivery"] = estimated_delivery

    apply_fees_logic(shipment, shipment.get("status", ""), fees_amount_raw, fees_reason_raw, clear_fees)

    existing_snapshot = shipments.get(tracking_id, {}) or {}
    if should_regenerate_route(existing_snapshot, shipment):
        shipment["route"] = generate_route(shipment.get("origin"), shipment.get("destination"))

    add_status_event_if_changed(shipment, old_status, shipment.get("status", ""))

    new_est = shipment.get("estimated_delivery") or ""
    add_estimated_delivery_event_if_changed(shipment, old_est or "", new_est)

    ensure_auto_history(shipment)

    shipments[tracking_id] = shipment
    save_json(SHIPMENTS_FILE, shipments)
    return redirect(url_for("admin_panel"))


@app.route("/admin/approve/<email>", methods=["POST"])
def admin_approve_application(email):
    gate = require_admin(next_url=url_for("admin_panel"))
    if gate:
        return gate

    email = (email or "").strip().lower()
    password = request.form.get("password", "")
    admin_note = (request.form.get("admin_note") or "").strip()

    if not email or not password:
        return ("Email and password are required", 400)

    applications = load_json(APPLICATIONS_FILE, {})
    app_entry = applications.get(email)
    if not app_entry:
        return redirect(url_for("admin_panel"))

    users = load_json(USERS_FILE, {})
    if email not in users:
        users[email] = {
            "email": email,
            "name": app_entry.get("name", ""),
            "password": password,
            "role": "user",
            "active": True,
            "created_at": now_str(),
        }
        save_json(USERS_FILE, users)

    app_entry["status"] = "approved"
    app_entry["admin_note"] = admin_note
    applications[email] = app_entry
    save_json(APPLICATIONS_FILE, applications)

    return redirect(url_for("admin_panel"))


@app.route("/admin/reject/<email>", methods=["POST"])
def admin_reject_application(email):
    gate = require_admin(next_url=url_for("admin_panel"))
    if gate:
        return gate

    email = (email or "").strip().lower()
    admin_note = (request.form.get("admin_note") or "").strip()

    applications = load_json(APPLICATIONS_FILE, {})
    app_entry = applications.get(email)
    if not app_entry:
        return redirect(url_for("admin_panel"))

    app_entry["status"] = "rejected"
    app_entry["admin_note"] = admin_note
    applications[email] = app_entry
    save_json(APPLICATIONS_FILE, applications)

    return redirect(url_for("admin_panel"))


@app.route("/delete/<tracking_id>", methods=["POST"])
def delete_shipment(tracking_id):
    gate = require_admin(next_url=url_for("admin_panel"))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    if tracking_id in shipments:
        shipments.pop(tracking_id)
        save_json(SHIPMENTS_FILE, shipments)
    return redirect(url_for("admin_panel"))


@app.route("/verify_payment/<tracking_id>", methods=["POST"])
def verify_payment(tracking_id):
    gate = require_admin(next_url=url_for("admin_panel"))
    if gate:
        return gate

    shipments = load_json(SHIPMENTS_FILE, {})
    shipment = shipments.get(tracking_id)
    if not shipment:
        return redirect(url_for("admin_panel"))

    fees = shipment.get("fees")
    if fees and isinstance(fees, dict) and fees.get("payment_submitted") and not fees.get("paid", False):
        fees["paid"] = True
        fees["payment_submitted"] = False
        shipment["fees"] = fees

        old_status = shipment.get("status", "")
        shipment["status"] = "In Transit"

        shipment.setdefault("events", []).append({
            "date": now_str(),
            "location": "Admin Verification",
            "description": "Payment Verified - Shipment Released"
        })

        add_status_event_if_changed(shipment, old_status, shipment["status"])
        ensure_auto_history(shipment)

        shipments[tracking_id] = shipment
        save_json(SHIPMENTS_FILE, shipments)

    return redirect(url_for("admin_panel"))


# -----------------------
# Website pages
# -----------------------
@app.route("/contact")
def contact():
    return render_template("contact.html", user=current_user())

@app.route("/claims")
def claims():
    return render_template("claims.html", user=current_user())

@app.route("/privacy")
def privacy():
    return render_template("privacy.html", user=current_user())

@app.route("/prohibited-items")
def prohibited_items():
    return render_template("prohibited_items.html", user=current_user())

@app.route("/prohibited_items")
def prohibited_items_alias():
    return redirect(url_for("prohibited_items"))

@app.route("/quote")
def quote():
    return render_template("quote.html", user=current_user())

@app.route("/services")
def services():
    return render_template("services.html", user=current_user())

@app.route("/locations")
def locations():
    return render_template("locations.html", user=current_user())

@app.route("/policies")
def policies():
    return render_template("policies.html", user=current_user())

@app.route("/support")
def support():
    return render_template("support.html", user=current_user())

@app.route("/terms")
def terms():
    return render_template("terms.html", user=current_user())


if __name__ == "__main__":
    app.run(debug=True)
