import os
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler
import folium
import telebot
from dotenv import load_dotenv

# Load local environment variables
load_dotenv()

# Initialize Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

from werkzeug.middleware.proxy_fix import ProxyFix

# Initialize Flask
app = Flask(__name__)

# Apply ProxyFix middleware so the app handles headers correctly when run behind an Nginx reverse proxy
app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1
)

# Configure Database
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/spot_tracker.db")

# Automatically convert relative SQLite paths to absolute paths so they always work,
# regardless of what working directory or tool (like Gunicorn, IDE, or terminal) starts the app!
if DATABASE_URL.startswith("sqlite:///"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if db_path != ":memory:":
        if not db_path.startswith("/") and not db_path.startswith("\\"):
            base_dir = os.path.abspath(os.path.dirname(__file__))
            abs_db_path = os.path.abspath(os.path.join(base_dir, db_path))
            db_dir = os.path.dirname(abs_db_path)
            os.makedirs(db_dir, exist_ok=True)
            DATABASE_URL = f"sqlite:///{abs_db_path}"
            logger.info(f"SQLite relative path resolved to absolute path: {abs_db_path}")
        else:
            # If it is already absolute, make sure the directory exists
            db_dir = os.path.dirname(db_path)
            os.makedirs(db_dir, exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Configure Scheduler
app.config["SCHEDULER_API_ENABLED"] = False  # Disable external scheduling API for security
db = SQLAlchemy(app)
scheduler = APScheduler()

# Initialize Telegram Bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FEED_ID = os.getenv("FEED_ID", "0FOq6U5ICzOEL4qCqbM8YrAOqUzP8uGUp")
SPOT_FEED_URL = os.getenv(
    "SPOT_FEED_URL",
    "https://api.findmespot.com/spot-main-web/consumer/rest-api/2.0/public/feed/{FEED_ID}/message.json"
)
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", 60))

bot = None
if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    try:
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
        logger.info("Telegram Bot initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Telegram Bot: {e}")
else:
    logger.warning("Telegram Bot Token or Chat ID not configured. Alerts will be logged only.")


# Define Database Model
class Message(db.Model):
    __tablename__ = 'messages'

    id = db.Column(db.BigInteger, primary_key=True)
    messengerId = db.Column(db.String(50))
    messengerName = db.Column(db.String(100))
    modelId = db.Column(db.String(50))
    messageType = db.Column(db.String(50))
    dateTime = db.Column(db.String(100))
    unixTime = db.Column(db.Integer, index=True)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    altitude = db.Column(db.Float)
    batteryState = db.Column(db.String(20))
    messageContent = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Message {self.id} - {self.messageType} at {self.dateTime}>"


def send_telegram_alert(msg):
    """Formats and sends a telegram alert for a newly ingested track or custom message."""
    if not bot or not TELEGRAM_CHAT_ID:
        logger.info(f"Skipping Telegram notification (Bot not configured) for: {msg.id}")
        return

    # Use friendly format for times (extract YYYY-MM-DD HH:MM:SS)
    friendly_time = msg.dateTime
    try:
        # e.g., "2026-06-13T09:48:29+0000"
        dt = datetime.strptime(msg.dateTime, "%Y-%m-%dT%H:%M:%S%z")
        friendly_time = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        pass

    google_maps_url = f"https://maps.google.com/?q={msg.latitude},{msg.longitude}"

    if msg.messageType == "TRACK":
        text = (
            f"🛰️ *New Track Point Detected!*\n\n"
            f"👤 *Device:* {msg.messengerName} ({msg.modelId})\n"
            f"🏷️ *Type:* {msg.messageType}\n"
            f"📅 *Time:* {friendly_time}\n"
            f"📍 *Coordinates:* `{msg.latitude}, {msg.longitude}`\n\n"
            f"🗺️ [View on Google Maps]({google_maps_url})\n"
            f"🌐 [View Live Track Board](https://livetrackboard.ydns.eu/)"
        )
    else:  # CUSTOM, OK, etc.
        content = msg.messageContent or "No custom text content provided."
        text = (
            f"👤 *Device:* {msg.messengerName} ({msg.modelId})\n"
            f"📝 *Message:* \"{content}\"\n"
            f"🏷️ *Type:* {msg.messageType}\n"
            f"📅 *Time:* {friendly_time}\n"
            f"📍 *Coordinates:* `{msg.latitude}, {msg.longitude}`\n\n"
            f"🗺️ [View on Google Maps]({google_maps_url})\n"
            f"🌐 [View Live Track Board](https://livetrackboard.ydns.eu/)"
        )

    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="Markdown", disable_web_page_preview=False)
        logger.info(f"Telegram notification sent for message ID {msg.id}")
    except Exception as e:
        logger.error(f"Error sending Telegram notification: {e}")


def send_startup_notification():
    """Sends a startup test notification to the Telegram channel on application launch."""
    if not bot or not TELEGRAM_CHAT_ID:
        logger.info("Skipping Telegram startup notification (Bot not configured).")
        return

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"🚀 *SPOT Satellite Tracker Started Successfully!*\n\n"
        f"📊 *Current Status:*\n"
        f"📡 *Feed ID:* `{FEED_ID}`\n"
        f"🔄 *Scraper Interval:* `{SCRAPE_INTERVAL}s`\n"
        f"📁 *Database:* `SQLite`\n"
        f"📅 *Startup Time:* `{current_time} UTC`\n\n"
        f"Ready to track satellite data and deliver notifications!"
    )
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="Markdown")
        logger.info("Telegram startup test notification sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send Telegram startup notification: {e}")


def scrape_spot_feed():
    """Scrapes the SPOT satellite tracker JSON endpoint and upserts data into SQLite."""
    # Run database tasks inside Flask Application Context
    with app.app_context():
        formatted_url = SPOT_FEED_URL.format(FEED_ID=FEED_ID)
        logger.info(f"Fetching SPOT satellite data from: {formatted_url}")

        try:
            response = requests.get(formatted_url, timeout=15)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Failed to fetch or parse SPOT API response: {e}")
            return

        response_obj = data.get("response", {})
        feed_message_response = response_obj.get("feedMessageResponse", {})
        messages_container = feed_message_response.get("messages", {})

        if not messages_container:
            logger.info("No messages found in the SPOT API response.")
            return

        messages_list = messages_container.get("message", [])
        # SPOT API returns a single dictionary if there's only one message
        if isinstance(messages_list, dict):
            messages_list = [messages_list]

        logger.info(f"Retrieved {len(messages_list)} messages from feed. Processing...")

        new_count = 0
        for msg_data in reversed(messages_list):  # Process oldest to newest to ensure logical order
            try:
                msg_id = int(msg_data["id"])
                # Check for duplicates
                existing = db.session.get(Message, msg_id)
                if existing is not None:
                    continue

                # Parse and build message object
                # Some fields might be missing or have different types, so use default safety
                new_msg = Message(
                    id=msg_id,
                    messengerId=str(msg_data.get("messengerId", "")),
                    messengerName=str(msg_data.get("messengerName", "")),
                    modelId=str(msg_data.get("modelId", "")),
                    messageType=str(msg_data.get("messageType", "")),
                    dateTime=str(msg_data.get("dateTime", "")),
                    unixTime=int(msg_data.get("unixTime", 0)),
                    latitude=float(msg_data.get("latitude", 0.0)),
                    longitude=float(msg_data.get("longitude", 0.0)),
                    altitude=float(msg_data.get("altitude", 0.0)),
                    batteryState=str(msg_data.get("batteryState", "UNKNOWN")),
                    messageContent=msg_data.get("messageContent")
                )

                db.session.add(new_msg)
                db.session.commit()
                new_count += 1
                logger.info(f"Saved new message ID {msg_id} (Type: {new_msg.messageType})")

                # Send Telegram notifications under specific conditions:
                # - Send on every custom message (CUSTOM, OK).
                # - Send for TRACK message only if it is the first TRACK of the day
                #   OR if the latest message immediately preceding it was a custom message (CUSTOM, OK).
                should_notify = False
                if new_msg.messageType in ["CUSTOM", "OK"]:
                    should_notify = True
                elif new_msg.messageType == "TRACK":
                    # Check if first TRACK event of the day
                    dt = datetime.fromtimestamp(new_msg.unixTime, tz=timezone.utc)
                    date_str = dt.strftime("%Y-%m-%d")
                    start_unix, _ = parse_date_to_unix_bounds(date_str)
                    
                    is_first_track = False
                    if start_unix is not None:
                        earlier_track = Message.query.filter(
                            Message.messageType == "TRACK",
                            Message.unixTime >= start_unix,
                            Message.unixTime < new_msg.unixTime
                        ).first()
                        if earlier_track is None:
                            is_first_track = True

                    # Check if latest event before this one is custom (CUSTOM, OK)
                    prev_msg = Message.query.filter(
                        Message.unixTime < new_msg.unixTime
                    ).order_by(Message.unixTime.desc()).first()
                    
                    is_prev_custom = False
                    if prev_msg and prev_msg.messageType in ["CUSTOM", "OK"]:
                        is_prev_custom = True
                        
                    if is_first_track or is_prev_custom:
                        should_notify = True

                if should_notify:
                    send_telegram_alert(new_msg)

            except Exception as e:
                db.session.rollback()
                logger.error(f"Error processing message data {msg_data.get('id')}: {e}")

        logger.info(f"Feed scraping finished. Added {new_count} new messages.")


# Initialize Scheduler Job
@scheduler.task('interval', id='scrape_spot_job', seconds=SCRAPE_INTERVAL, misfire_grace_time=900)
def scheduled_scrape():
    logger.info("Executing scheduled SPOT scraping job...")
    scrape_spot_feed()


# Parse Date bounds helper
def parse_date_to_unix_bounds(date_str):
    """
    Given a YYYY-MM-DD date string, returns (start_unix, end_unix)
    spanning that full calendar day.
    """
    try:
        # User input is in YYYY-MM-DD
        dt_start = datetime.strptime(f"{date_str}T00:00:00", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        dt_end = datetime.strptime(f"{date_str}T23:59:59", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt_start.timestamp()), int(dt_end.timestamp())
    except Exception as e:
        logger.error(f"Error parsing user date range: {e}")
        return None, None


# Web App Routing
@app.route("/")
def index():
    # Fetch parameters if submitted via the Date Picker form
    start_date = request.args.get("startDate", "")

    # Set default values if not selected by user
    if not start_date:
        # Fallback to the latest message's date
        latest_msg = Message.query.order_by(Message.unixTime.desc()).first()
        if latest_msg:
            try:
                # Get the day from unix time
                dt = datetime.fromtimestamp(latest_msg.unixTime, tz=timezone.utc)
                start_date = dt.strftime("%Y-%m-%d")
            except Exception:
                # Default fallback to current day
                start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        else:
            # Empty database fallback
            start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Get latest message metadata to show quick stats on dashboard
    latest_msg = Message.query.order_by(Message.unixTime.desc()).first()

    # Query all events of the selected date to display at the bottom of the page
    start_unix, _ = parse_date_to_unix_bounds(start_date)
    events_data = []
    if start_unix is not None:
        end_unix = start_unix + 86400
        events = Message.query.filter(
            Message.unixTime >= start_unix,
            Message.unixTime <= end_unix
        ).order_by(Message.unixTime.asc()).all()

        for event in events:
            friendly_time = event.dateTime
            try:
                dt = datetime.strptime(event.dateTime, "%Y-%m-%dT%H:%M:%S%z")
                friendly_time = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                pass
            
            events_data.append({
                "id": event.id,
                "time": friendly_time,
                "type": event.messageType,
                "latitude": event.latitude,
                "longitude": event.longitude,
                "altitude": int(event.altitude),
                "battery": event.batteryState,
                "content": event.messageContent or ""
            })

    return render_template(
        "index.html",
        start_date=start_date,
        latest_msg=latest_msg,
        events=events_data
    )


@app.route("/map")
def map_view():
    """Generates and returns the Folium Map HTML based on the startDate parameter."""
    start_date_param = request.args.get("startDate", "")

    start_unix, end_unix = None, None

    if start_date_param:
        # Convert YYYY-MM-DD parameter to unix bounds (start of day)
        su, _ = parse_date_to_unix_bounds(start_date_param)
        if su is not None:
            start_unix = su
            end_unix = su + 86400  # Exactly 24 hours later (86400 seconds)

    # If no parameters, query the last available day from the database
    if not start_unix or not end_unix:
        latest_msg = Message.query.order_by(Message.unixTime.desc()).first()
        if latest_msg:
            try:
                dt = datetime.fromtimestamp(latest_msg.unixTime, tz=timezone.utc)
                date_str = dt.strftime("%Y-%m-%d")
                su, _ = parse_date_to_unix_bounds(date_str)
                if su is not None:
                    start_unix = su
                    end_unix = su + 86400
            except Exception:
                pass

    # Query the database
    if start_unix and end_unix:
        points = Message.query.filter(
            Message.unixTime >= start_unix,
            Message.unixTime <= end_unix
        ).order_by(Message.unixTime.asc()).all()
    else:
        points = []

    # Map centering setup
    if points:
        # Center map on the latest point of this track
        center_lat = points[-1].latitude
        center_lon = points[-1].longitude
        zoom_level = 13
    else:
        # Default global zoom if no points
        center_lat, center_lon = 32.0, 34.8
        zoom_level = 3

    # Generate Folium Map
    folium_map = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom_level,
        tiles=None,
        control_scale=True
    )

    # Add multiple standard built-in map layers (CartoDB Positron first as default)
    folium.TileLayer("CartoDB Positron", name="CartoDB Positron (Light)").add_to(folium_map)
    folium.TileLayer("OpenStreetMap", name="Open Street Map").add_to(folium_map)
    folium.TileLayer("CartoDB Dark_Matter", name="CartoDB Dark Matter (Dark)").add_to(folium_map)

    # Add Layer Toggle Control widget on the map UI
    folium.LayerControl(position="topright").add_to(folium_map)

    if points:
        # Draw path polyline connecting all points
        coords = [(pt.latitude, pt.longitude) for pt in points]
        folium.PolyLine(
            locations=coords,
            color="#2a82c9",
            weight=5,
            opacity=0.85,
            tooltip="Device Track Line"
        ).add_to(folium_map)

        # Plot points with special icons/styles
        for i, pt in enumerate(points):
            friendly_time = pt.dateTime
            try:
                dt = datetime.strptime(pt.dateTime, "%Y-%m-%dT%H:%M:%S%z")
                friendly_time = dt.strftime("%Y-%m-%d %H:%M:%S Local")
            except Exception:
                pass

            popup_html = (
                f"<div style='font-family: sans-serif; font-size: 13px; line-height: 1.4; color: #333; min-width: 180px;'>"
                f"<strong style='color:#0f52ba; font-size: 14px;'>📍 Track Point</strong><br>"
                f"<b>ID:</b> {pt.id}<br>"
                f"<b>Device:</b> {pt.messengerName}<br>"
                f"<b>Time:</b> {friendly_time}<br>"
                f"<b>Alt:</b> {int(pt.altitude)}m<br>"
                f"<b>Battery:</b> {pt.batteryState}<br>"
                f"<b>Type:</b> {pt.messageType}"
            )
            if pt.messageContent:
                popup_html += f"<br><b style='color:#e67e22;'>Msg:</b> \"{pt.messageContent}\""
            
            # Google Maps Link inside pop-up
            gmaps_url = f"https://maps.google.com/?q={pt.latitude},{pt.longitude}"
            popup_html += f"<br><br><a href='{gmaps_url}' target='_blank' rel='noopener noreferrer' style='color:#3b82f6; text-decoration:none; font-weight:600;'>🗺️ View on Google Maps</a>"
            popup_html += "</div>"

            # 1. Start point of this day's track
            if i == 0:
                folium.Marker(
                    location=[pt.latitude, pt.longitude],
                    popup=folium.Popup(popup_html, max_width=250),
                    tooltip="🏁 Start of Day Track",
                    icon=folium.Icon(color="green", icon="play", prefix="fa")
                ).add_to(folium_map)

            # 2. Custom Events or Check-Ins (CUSTOM / OK)
            elif pt.messageType in ["CUSTOM", "OK"]:
                folium.Marker(
                    location=[pt.latitude, pt.longitude],
                    popup=folium.Popup(popup_html, max_width=250),
                    tooltip=f"💬 Custom Event: {pt.messageType}",
                    icon=folium.Icon(color="orange", icon="envelope", prefix="fa")
                ).add_to(folium_map)

            # 3. Regular path points
            else:
                folium.CircleMarker(
                    location=[pt.latitude, pt.longitude],
                    radius=5,
                    color="#2a82c9",
                    fill=True,
                    fill_color="#5dade2",
                    fill_opacity=0.8,
                    popup=folium.Popup(popup_html, max_width=250)
                ).add_to(folium_map)

    # Render raw HTML of the map to return
    return folium_map.get_root().render()


# Run initialization on startup
with app.app_context():
    # Create SQLite tables
    db.create_all()
    logger.info("Database tables initialized.")

    # Always fetch immediately on startup to get the latest coordinates in database
    if os.getenv("FLASK_ENV") != "testing":
        logger.info("Running initial SPOT feed scrape on startup...")
        scrape_spot_feed()
        
        logger.info("Sending Telegram startup notification...")
        send_startup_notification()

# Start background scheduler
scheduler.init_app(app)
scheduler.start()
logger.info("Background scheduler started successfully.")

if __name__ == "__main__":
    # For local running outside Gunicorn
    app.run(host="0.0.0.0", port=5000, debug=True)
